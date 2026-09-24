"""Durable reminder creation/cancellation and delivery handler (SPEC §8).

Reminders are persisted as rows plus a durable background job, so a worker
restart cannot lose a pending send. Delivery is idempotent: the handler only
acts on reminders still in the ``pending`` state, and ``sent_at`` is written
exactly once. ``create_job`` is idempotent on ``idempotency_key`` so a
retried create cannot enqueue a second delivery job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from assistant.i18n import DEFAULT_LANGUAGE, t
from assistant.models.calendar_items import CalendarItem
from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.models.reminders import Reminder, ReminderStatus
from assistant.models.users import User
from assistant.services import notifications
from assistant.services.jobs import (
    LeaseLostError,
    cancel_job,
    create_job,
    current_lease,
)

REMINDER_SEND_JOB_TYPE = "reminder_send"

# Shared reminder-offset bounds (SPEC §14.2): one gate for the API, action,
# and bot creation paths. 0 fires at the start; the bounds are symmetric
# over a day (the DRAFT_SYSTEM prompt documents negative offsets as
# "fire after the start"), so NLU drafts stay valid while runaway lists and
# absurd offsets are rejected.
MAX_REMINDERS_PER_ITEM = 5
MIN_OFFSET_MINUTES = -24 * 60
MAX_OFFSET_MINUTES = 24 * 60

#: Hard cap on the candidates a free-text entity resolution will load
#: (V4 §23) — mirrors :data:`assistant.services.calendar._RESOLVE_CANDIDATE_LIMIT`.
_RESOLVE_CANDIDATE_LIMIT = 25


def _escape_like(query: str) -> str:
    """Escape LIKE metacharacters so a substring match stays a literal search."""
    return query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _idempotency_key(reminder_id: int) -> str:
    return f"reminder:{reminder_id}"


def validate_reminder_offsets(offsets_minutes: list[int]) -> list[int]:
    """Shared reminder-offset validation (SPEC §14.2).

    Deduplicates offsets and enforces the numeric bounds and the maximum
    number of reminders per item. Returns the deduplicated list in input
    order. Raises ValueError for a non-integer offset, an out-of-bounds
    offset, or more than ``MAX_REMINDERS_PER_ITEM`` unique offsets.
    """
    seen: set[int] = set()
    unique: list[int] = []
    for offset in offsets_minutes:
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValueError(f"Reminder offset must be an integer (got {offset!r}).")
        if offset < MIN_OFFSET_MINUTES or offset > MAX_OFFSET_MINUTES:
            raise ValueError(
                f"Reminder offset must be between {MIN_OFFSET_MINUTES} and "
                f"{MAX_OFFSET_MINUTES} minutes (got {offset})."
            )
        if offset in seen:
            continue
        seen.add(offset)
        unique.append(offset)
    if len(unique) > MAX_REMINDERS_PER_ITEM:
        raise ValueError(
            f"At most {MAX_REMINDERS_PER_ITEM} reminders per item "
            f"(got {len(unique)} unique offsets)."
        )
    return unique


async def create_reminder(
    session: AsyncSession,
    user: User,
    *,
    fire_at: datetime,
    message: str,
    calendar_item: CalendarItem | None = None,
    trigger_type: str = "absolute",
    offset_minutes: int | None = None,
) -> Reminder:
    """Create a pending reminder and enqueue its durable delivery job.

    ``fire_at`` must be a UTC-aware datetime (the effective fire time). For
    ``item_linked`` reminders, also pass the linked item and the offset used
    to resolve ``fire_at``.
    """
    if not message.strip():
        raise ValueError("Reminder message is required.")
    if len(message) > 1000:
        raise ValueError("Reminder message is too long (max 1000 characters).")
    if fire_at.tzinfo is None:
        raise ValueError("fire_at must be timezone-aware (UTC).")
    if calendar_item is not None and calendar_item.user_id != user.id:
        raise ValueError("Reminder cannot be linked to another user's item.")

    reminder = Reminder(
        user_id=user.id,
        calendar_item_id=calendar_item.id if calendar_item is not None else None,
        trigger_type=trigger_type,
        offset_minutes=offset_minutes,
        fire_at=fire_at,
        message=message.strip(),
        status=ReminderStatus.pending.value,
    )
    session.add(reminder)
    await session.flush()

    job = await create_job(
        session,
        type=REMINDER_SEND_JOB_TYPE,
        payload={"reminder_id": reminder.id},
        user_id=user.id,
        idempotency_key=_idempotency_key(reminder.id),
        available_at=fire_at,
    )
    reminder.job_id = job.id
    await session.flush()
    return reminder


async def create_item_reminders(
    session: AsyncSession,
    user: User,
    item: CalendarItem,
    *,
    offsets_minutes: list[int],
) -> list[Reminder]:
    """Create ``item_linked`` reminders at each offset before the item.

    An offset of ``0`` fires at ``item.starts_at``. Offsets are only applied
    when the item has a ``starts_at``; otherwise the item is skipped.
    Offsets pass the shared SPEC §14.2 validation (bounds, deduplication,
    maximum per item). The maximum is enforced over the item's total pending
    ``item_linked`` reminders, so a second create cannot push an item past
    the cap (V4 §28). Returns the created reminders (empty when the item
    has no ``starts_at``).
    """
    unique = validate_reminder_offsets(offsets_minutes)
    # V5 §7: serialize concurrent creates on the same item by locking its
    # row, and dedupe the requested offsets against the item's *existing*
    # pending ``item_linked`` reminders so a retried / second create cannot
    # duplicate an offset the item already has (the old code only counted,
    # so two concurrent creates could both pass the count check and insert
    # the same offset twice).
    locked = await session.get(CalendarItem, item.id, with_for_update=True)
    if locked is None:
        raise ValueError("Calendar item no longer exists.")
    item = locked
    existing_offsets = set(
        (
            await session.scalars(
                select(Reminder.offset_minutes).where(
                    Reminder.calendar_item_id == item.id,
                    Reminder.status == ReminderStatus.pending.value,
                    Reminder.trigger_type == "item_linked",
                )
            )
        ).all()
    )
    new_offsets = [o for o in unique if o not in existing_offsets]
    union = existing_offsets | set(unique)
    # The cap is enforced on the union (existing + requested, deduplicated),
    # so a second create cannot push an item past MAX_REMINDERS_PER_ITEM.
    if len(union) > MAX_REMINDERS_PER_ITEM:
        raise ValueError(
            f"At most {MAX_REMINDERS_PER_ITEM} reminders per item "
            f"({len(existing_offsets)} already scheduled; "
            f"{len(new_offsets)} new requested)."
        )
    if item.starts_at is None:
        return []
    created: list[Reminder] = []
    for offset in new_offsets:
        fire_at = item.starts_at - timedelta(minutes=offset)
        reminder = await create_reminder(
            session,
            user,
            fire_at=fire_at,
            # Store the user's text only; the localized "Reminder:" wrapper
            # is applied at delivery time in the user's current language.
            message=item.title,
            calendar_item=item,
            trigger_type="item_linked",
            offset_minutes=offset,
        )
        created.append(reminder)
    return created


async def reschedule_item_reminders(
    session: AsyncSession, user: User, item: CalendarItem
) -> int:
    """Recompute pending ``item_linked`` reminders after the item changes.

    Called from the shared calendar service when the item's start time (or
    title) changes, so the invariant "linked reminders fire at
    ``starts_at - offset``" holds no matter which surface (bot, API,
    action engine) performed the update. If the item no longer has a start,
    the pending linked reminders are cancelled — there is nothing to anchor
    them to. Returns the number of reminders rescheduled (0 when the start
    was cleared or there were no pending linked reminders).
    """
    reminders = (
        await session.scalars(
            select(Reminder).where(
                Reminder.user_id == user.id,
                Reminder.calendar_item_id == item.id,
                Reminder.status == ReminderStatus.pending.value,
                Reminder.trigger_type == "item_linked",
            )
        )
    ).all()
    if not reminders:
        return 0
    if item.starts_at is None:
        now = datetime.now(UTC)
        for reminder in reminders:
            reminder.status = ReminderStatus.cancelled.value
            reminder.cancelled_at = now
            if reminder.job_id is not None:
                await cancel_job(session, reminder.job_id)
        await session.flush()
        return 0
    for reminder in reminders:
        offset = reminder.offset_minutes or 0
        reminder.fire_at = item.starts_at - timedelta(minutes=offset)
        reminder.message = item.title
        if reminder.job_id is not None:
            job = await session.get(BackgroundJob, reminder.job_id)
            if job is not None and job.status == JobStatus.pending.value:
                job.available_at = reminder.fire_at
    await session.flush()
    return len(reminders)


async def get_reminder(
    session: AsyncSession, user: User, reminder_id: int
) -> Reminder | None:
    """Return a reminder belonging to the user, if any."""
    reminder = await session.get(Reminder, reminder_id)
    if reminder is None or reminder.user_id != user.id:
        return None
    return reminder


async def list_reminders(
    session: AsyncSession,
    user: User,
    *,
    status: ReminderStatus | None = None,
    item_id: int | None = None,
    limit: int = 100,
) -> list[Reminder]:
    """List the user's reminders ordered by fire time.

    ``item_id`` restricts the list to reminders linked to one calendar item.
    """
    stmt = (
        select(Reminder)
        .where(Reminder.user_id == user.id)
        .order_by(Reminder.fire_at, Reminder.id)
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(Reminder.status == status.value)
    if item_id is not None:
        stmt = stmt.where(Reminder.calendar_item_id == item_id)
    return list((await session.scalars(stmt)).all())


async def resolve_reminder(
    session: AsyncSession, user: User, text: str
) -> tuple[Reminder | None, list[Reminder]]:
    """Deterministically resolve a free-text reference to a reminder.

    Case-insensitive exact message match, then substring, over ``pending``
    reminders only. Returns ``(best, candidates)`` with the same semantics
    as :func:`assistant.services.calendar.resolve_item`: a unique match, an
    ambiguous candidate list (ordered by fire time then id), or no match.

    Candidate selection is bounded at the SQL layer (V4 §23): exact match
    first, then a bounded substring search, each returning at most
    :data:`_RESOLVE_CANDIDATE_LIMIT` rows — the resolver never loads an
    unbounded reminder history into Python.
    """
    query = text.strip().casefold()
    if not query:
        return None, []
    base = select(Reminder).where(
        Reminder.user_id == user.id,
        Reminder.status == ReminderStatus.pending.value,
    )

    def _candidates(match) -> Select[Reminder]:
        return (
            base.where(match)
            .order_by(Reminder.fire_at, Reminder.id)
            .limit(_RESOLVE_CANDIDATE_LIMIT)
        )

    message = func.lower(Reminder.message)
    # Phase 1: exact case-insensitive match.
    rows = list((await session.execute(_candidates(message == query))).scalars().all())
    if not rows:
        # Phase 2: bounded substring match (metachars escaped to stay literal).
        like = f"%{_escape_like(query)}%"
        rows = list(
            (await session.execute(_candidates(message.like(like, escape="\\")))).scalars().all()
        )
    if len(rows) == 1:
        return rows[0], rows
    return (None, rows) if rows else (None, [])


async def cancel_reminder(
    session: AsyncSession, user: User, reminder_id: int
) -> Reminder | None:
    """Cancel a pending reminder and its delivery job (idempotent)."""
    reminder = await get_reminder(session, user, reminder_id)
    if reminder is None:
        return None
    if reminder.status == ReminderStatus.pending.value:
        reminder.status = ReminderStatus.cancelled.value
        reminder.cancelled_at = datetime.now(UTC)
        if reminder.job_id is not None:
            await cancel_job(session, reminder.job_id)
        await session.flush()
    return reminder


async def cancel_item_reminders(
    session: AsyncSession, user: User, item_id: int
) -> int:
    """Cancel all pending reminders linked to a calendar item.

    Returns the number of reminders cancelled. Reminders already sent or
    cancelled are left untouched.
    """
    reminders = (
        await session.scalars(
            select(Reminder).where(
                Reminder.user_id == user.id,
                Reminder.calendar_item_id == item_id,
                Reminder.status == ReminderStatus.pending.value,
            )
        )
    ).all()
    for reminder in reminders:
        reminder.status = ReminderStatus.cancelled.value
        reminder.cancelled_at = datetime.now(UTC)
        if reminder.job_id is not None:
            await cancel_job(session, reminder.job_id)
    if reminders:
        await session.flush()
    return len(reminders)


async def _handle_reminder_send(
    session: AsyncSession, job: BackgroundJob
) -> None:
    """Deliver a reminder through the Bot API, then mark it sent.

    Delivery semantics (SPEC §6.1, §6.3): **durable at-least-once**. The
    ``sent_at`` stamp is written only AFTER the send succeeds, so a crash
    between the two re-sends the reminder (a duplicate the user may see)
    rather than losing it. No database transaction is held while waiting on
    the Telegram network call.

    The handler owns its transaction boundaries (SPEC §5): a short read
    transaction to load the reminder, a send with no transaction open, and a
    short final transaction to stamp ``sent_at``. If the send fails the
    exception propagates so the job re-queues with backoff and the reminder
    stays ``pending``.
    """
    if job.status != JobStatus.running.value:
        return
    reminder_id = job.payload.get("reminder_id")
    if reminder_id is None:
        raise ValueError("reminder_send job payload is missing reminder_id")
    # Phase A — short read transaction.
    reminder = await session.get(Reminder, reminder_id)
    if reminder is None:
        # Item (and with it the reminder) was deleted after the job was
        # queued; nothing to deliver.
        return
    if reminder.status != ReminderStatus.pending.value:
        # Already sent (retry/restart) or cancelled: idempotent no-op.
        return
    user = await session.get(
        User, reminder.user_id, options=[selectinload(User.settings)]
    )
    if user is None:
        return
    # The wrapper is localized at execution time so a later language change
    # takes effect; the user's own reminder text is sent unchanged.
    language = (
        user.settings.language if user.settings is not None else DEFAULT_LANGUAGE
    )
    chat_id = user.id
    message = t(language, "reminders.notification", message=reminder.message)
    await session.commit()  # release the connection before the network call

    # Do not send from a stale lease: a new owner (or recovery) will deliver
    # it — delivery is durable at-least-once, so this just avoids a duplicate
    # (V4 §8).
    lease = current_lease.get()
    if lease is not None:
        try:
            lease.raise_if_lost()
        except LeaseLostError:
            return

    # Phase B — Telegram send, with no transaction held.
    await notifications.send_text(chat_id, message)

    # Phase C — short transaction; stamp sent exactly once (re-fetch so a
    # concurrent cancel between A and C is not clobbered).
    reminder = await session.get(Reminder, reminder_id, populate_existing=True)
    if reminder is not None and reminder.status == ReminderStatus.pending.value:
        reminder.status = ReminderStatus.sent.value
        reminder.sent_at = datetime.now(UTC)
        await session.commit()


def _register_handler() -> None:
    """Register the delivery handler with the worker's handler registry."""
    from assistant.worker.registry import register_job_handler

    register_job_handler(REMINDER_SEND_JOB_TYPE)(_handle_reminder_send)


_register_handler()
