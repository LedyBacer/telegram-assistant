"""Durable daily morning digest (SPEC §16) and motivation line (SPEC §17).

Idempotency: exactly one ``DigestDelivery`` row per (user, local date) —
enforced by the ``uq_digests_user_day`` unique constraint — and exactly one
``digest_send`` job per delivery, keyed ``digest:{user_id}:{date}``. A
worker restart therefore never creates duplicate digests.

The worker calls :func:`ensure_digest_jobs` periodically; the actual send
happens in the ``digest_send`` job handler, which delivers through the
Bot API (``notifications``) and stamps ``sent_at`` exactly once.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from assistant.i18n import DEFAULT_LANGUAGE, t
from assistant.models.calendar_items import CalendarItem
from assistant.models.digests import DigestDelivery
from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.models.users import User
from assistant.services import calendar as calendar_service
from assistant.services import jobs as jobs_service
from assistant.services import motivation, notifications
from assistant.services import workouts as workouts_service

DIGEST_JOB_TYPE = "digest_send"

TODAY_LIMIT = 10
OVERDUE_LIMIT = 5
UPCOMING_LIMIT = 5


def _user_tz(user: User) -> ZoneInfo:
    name = user.settings.timezone if user.settings is not None else "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError):
        return ZoneInfo("UTC")


def _user_lang(user: User) -> str:
    return user.settings.language if user.settings is not None else DEFAULT_LANGUAGE


def _idempotency_key(user_id: int, digest_date: date) -> str:
    return f"digest:{user_id}:{digest_date.isoformat()}"


def _item_line(item: CalendarItem, language: str) -> str:
    parts = [t(language, "digest.item", title=item.title, kind=item.kind.value)]
    if item.starts_at is not None:
        parts.append(t(language, "digest.item_starts", when=item.starts_at.isoformat()))
    if item.due_at is not None:
        parts.append(t(language, "digest.item_due", when=item.due_at.isoformat()))
    return ", ".join(parts)


async def build_digest(session: AsyncSession, user: User) -> str:
    """Render the digest text from current application state."""
    tz = _user_tz(user)
    language = _user_lang(user)
    today = datetime.now(tz=tz).date()
    today_items = (await calendar_service.list_today(session, user))[:TODAY_LIMIT]
    overdue_items = (await calendar_service.list_overdue(session, user))[:OVERDUE_LIMIT]
    upcoming_items = (
        await calendar_service.list_upcoming(session, user, days=7)
    )[:UPCOMING_LIMIT]
    stats = await workouts_service.workout_stats(session, user)

    sections: list[str] = [
        t(language, "digest.title", name=user.first_name, date=today.isoformat())
    ]
    if today_items:
        sections.append(
            t(language, "digest.today") + "\n" + "\n".join(_item_line(i, language) for i in today_items)
        )
    else:
        sections.append(t(language, "digest.today_empty"))
    if overdue_items:
        sections.append(
            t(language, "digest.overdue") + "\n" + "\n".join(_item_line(i, language) for i in overdue_items)
        )
    if upcoming_items:
        sections.append(
            t(language, "digest.upcoming")
            + "\n"
            + "\n".join(_item_line(i, language) for i in upcoming_items)
        )
    if stats["total"]:
        sections.append(
            t(
                language,
                "digest.workouts",
                week=stats["this_week"],
                streak=stats["current_streak"],
            )
        )

    state = motivation.MotivationState(
        has_overdue=bool(overdue_items),
        has_upcoming_workout=any(
            i.source == "workout" for i in upcoming_items
        ),
        current_streak=stats["current_streak"],
        schedule_empty=not today_items and not overdue_items and not upcoming_items,
    )
    line = motivation.motivational_line(user, state)
    if line is not None:
        sections.append(line)
    return "\n\n".join(sections)


async def schedule_todays_digest(
    session: AsyncSession, user: User
) -> tuple[DigestDelivery, bool]:
    """Ensure a digest is scheduled for the user's local today.

    Returns the delivery and whether it was created by this call. A digest
    whose configured time has already passed is enqueued immediately (due
    digests are delivered late rather than dropped).
    """
    tz = _user_tz(user)
    today = datetime.now(tz=tz).date()
    # INSERT ... ON CONFLICT DO NOTHING: a lost uniqueness race returns an
    # empty result instead of raising IntegrityError. The old catch-and-
    # rollback path destroyed the caller's open transaction — in the worker's
    # multi-user ``ensure_digest_jobs`` pass that silently discarded every
    # delivery/job row flushed for earlier users.
    result = await session.execute(
        pg_insert(DigestDelivery)
        .values(user_id=user.id, digest_date=today)
        .on_conflict_do_nothing(index_elements=["user_id", "digest_date"])
        .returning(DigestDelivery.id)
    )
    new_id = result.scalar_one_or_none()
    if new_id is None:
        # A concurrent scheduler beat us to it; use the winning row.
        existing = await session.scalar(
            select(DigestDelivery).where(
                DigestDelivery.user_id == user.id,
                DigestDelivery.digest_date == today,
            )
        )
        if existing is None:
            raise RuntimeError(
                f"digest row for user {user.id} vanished after insert conflict"
            )
        return existing, False
    delivery = await session.get(DigestDelivery, new_id)
    assert delivery is not None

    digest_time = (
        user.settings.digest_time
        if user.settings is not None
        else time(8, 0)
    )
    fire_at = datetime.combine(today, digest_time, tzinfo=tz)
    job = await jobs_service.create_job(
        session,
        type=DIGEST_JOB_TYPE,
        payload={"digest_id": delivery.id},
        user_id=user.id,
        idempotency_key=_idempotency_key(user.id, today),
        available_at=fire_at.astimezone(UTC),
    )
    delivery.job_id = job.id
    await session.flush()
    return delivery, True


async def ensure_digest_jobs(session: AsyncSession) -> int:
    """Schedule today's digest for every user that does not have one yet.

    Called periodically by the worker. Returns the number of deliveries
    created by this pass.

    ``User.settings`` is eager-loaded: ``schedule_todays_digest`` reads the
    settings synchronously (timezone, digest time) and a lazy load from an
    async session outside greenlet context raises ``MissingGreenlet``.
    """
    users = (
        await session.scalars(
            select(User).options(selectinload(User.settings)).order_by(User.id)
        )
    ).all()
    created = 0
    for user in users:
        _, is_new = await schedule_todays_digest(session, user)
        created += 1 if is_new else 0
    return created


async def _handle_digest_send(session: AsyncSession, job: BackgroundJob) -> None:
    """Deliver the digest for this job (durable at-least-once, SPEC §6.1).

    The ``sent_at`` stamp is written only AFTER the send succeeds, so a crash
    between the two re-sends the digest (a duplicate) rather than losing it.
    No database transaction is held while waiting on the Telegram network
    call (SPEC §6.3). The handler owns its transaction boundaries (SPEC §5):
    a short read transaction builds the content, a send runs with no
    transaction open, and a short final transaction stamps ``sent_at``.
    """
    if job.status != JobStatus.running.value:
        return
    digest_id = job.payload.get("digest_id")
    if digest_id is None:
        raise ValueError("digest_send job payload is missing digest_id")
    # Phase A — short read transaction.
    delivery = await session.get(DigestDelivery, digest_id)
    if delivery is None:
        # User (and with it the delivery) was deleted after the job queued.
        return
    if delivery.sent_at is not None:
        # Already delivered (retry/restart): idempotent no-op.
        return
    # Eager-load settings so the recipient's language is read at execution
    # time (a later language change must take effect for this digest).
    user = await session.get(
        User, delivery.user_id, options=[selectinload(User.settings)]
    )
    if user is None:
        return
    content = await build_digest(session, user)
    chat_id = user.id
    await session.commit()  # release the connection before the network call

    # Do not send from a stale lease (V5 §3): PostgreSQL is the source of
    # truth for ownership, and the in-memory flag can lag it. This fresh DB
    # check runs in its own short transaction that ended (committed) after
    # Phase A, so no transaction is held during the Telegram HTTP round-trip.
    # If we no longer own the job (recovered / re-claimed / cancelled) a new
    # owner will deliver it — delivery is durable at-least-once, so skipping
    # just avoids a duplicate.
    if not await jobs_service.owns_job(session, job.id, job.locked_by):
        return

    # Phase B — Telegram send, with no transaction held.
    await notifications.send_text(chat_id, content)

    # Phase C — short transaction; stamp sent exactly once (re-fetch so a
    # concurrent change between A and C is not clobbered).
    delivery = await session.get(DigestDelivery, digest_id, populate_existing=True)
    if delivery is not None and delivery.sent_at is None:
        delivery.content = content
        delivery.sent_at = datetime.now(UTC)
        await session.commit()


def _register_handler() -> None:
    from assistant.worker.registry import register_job_handler

    register_job_handler(DIGEST_JOB_TYPE)(_handle_digest_send)


_register_handler()
