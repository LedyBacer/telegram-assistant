"""Limited proactivity pass (worker-driven, deterministic, anti-spam gated).

Triggers are derived from application state only (no model calls):

- ``weekly_review``: on Monday (user's local time), once per ISO week.
- ``workout``: once per user-local day when the last completed workout is
  older than :data:`WORKOUT_STALE_HOURS` (or there is none).

Every candidate nudge passes the per-user anti-spam gates (enabled, quiet
hours in the user's timezone, max nudges per day, minimum interval) and is
deduplicated by a durable ``NudgeDelivery`` row per (user, kind, period), so
a worker restart never double-sends. The same pass expires pending actions
whose ``expires_at`` has passed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from assistant.i18n import DEFAULT_LANGUAGE, t
from assistant.models.pending_actions import ActionStatus, PendingAction
from assistant.models.proactivity import NudgeDelivery, NudgeKind, ProactiveSettings
from assistant.models.users import User, UserSettings
from assistant.models.workout_logs import WorkoutLog
from assistant.services.notifications import send_text

logger = logging.getLogger("assistant.proactivity")

#: A workout is "stale" once it is older than this (drives the workout nudge).
WORKOUT_STALE_HOURS = 48

NUDGE_TEXT_KEYS = {
    NudgeKind.weekly_review: "proactive.weekly_review",
    NudgeKind.workout: "proactive.workout",
}


def _user_tz(user_settings: UserSettings | None) -> ZoneInfo:
    name = user_settings.timezone if user_settings is not None else "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError):
        return ZoneInfo("UTC")


def _user_lang(user_settings: UserSettings | None) -> str:
    return (
        user_settings.language
        if user_settings is not None
        else DEFAULT_LANGUAGE
    )


def _in_quiet_hours(local_now: datetime, settings: ProactiveSettings) -> bool:
    """True when ``local_now`` falls inside the configured quiet window.

    The window may wrap midnight (default 22:00 -> 08:00).
    """
    current = local_now.time()
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start <= end:
        return start <= current < end
    return current >= start or current < end


async def get_proactive_settings(
    session: AsyncSession, user_id: int
) -> ProactiveSettings:
    """Return the user's proactivity settings, creating defaults on demand."""
    settings = await session.scalar(
        select(ProactiveSettings).where(ProactiveSettings.user_id == user_id)
    )
    if settings is None:
        settings = ProactiveSettings(user_id=user_id)
        session.add(settings)
        await session.flush()
    return settings


async def _nudge_sent(
    session: AsyncSession, user_id: int, kind: str, period_key: str
) -> bool:
    return (
        await session.scalar(
            select(func.count())
            .select_from(NudgeDelivery)
            .where(
                NudgeDelivery.user_id == user_id,
                NudgeDelivery.kind == kind,
                NudgeDelivery.period_key == period_key,
            )
        )
        or 0
    ) > 0


async def evaluate_user(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
    send=send_text,
) -> list[str]:
    """Evaluate all triggers for one user and send eligible nudges.

    Takes the user's primary key, not the ORM instance: run_proactive_pass
    interleaves commits/rollbacks, and ``Session.rollback`` expires every
    object in the session — any lazy attribute reload in async context
    would raise MissingGreenlet. All data is read via explicit SELECTs.

    Returns the list of nudge kinds actually sent this call (flush only for
    the delivery rows; the caller owns the transaction).
    """
    now = now or datetime.now(UTC)
    user_settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    settings = await get_proactive_settings(session, user_id)
    if not settings.enabled:
        return []

    tz = _user_tz(user_settings)
    now_local = now.astimezone(tz)
    if _in_quiet_hours(now_local, settings):
        return []

    local_date = now_local.date()
    # Convert the local day's start/end back to UTC for the counter query.
    day_start_utc = datetime.combine(local_date, time.min, tzinfo=tz).astimezone(UTC)
    day_end_utc = day_start_utc + timedelta(days=1)
    today_count = (
        await session.scalar(
            select(func.count())
            .select_from(NudgeDelivery)
            .where(
                NudgeDelivery.user_id == user_id,
                NudgeDelivery.sent_at >= day_start_utc,
                NudgeDelivery.sent_at < day_end_utc,
            )
        )
        or 0
    )
    if today_count >= settings.max_nudges_per_day:
        return []

    last = (
        await session.scalar(
            select(func.max(NudgeDelivery.sent_at)).where(
                NudgeDelivery.user_id == user_id
            )
        )
    )
    if (
        last is not None
        and now - last < timedelta(minutes=settings.min_interval_minutes)
    ):
        return []

    sent: list[str] = []
    language = _user_lang(user_settings)

    # Weekly review: Monday in the user's local time, once per ISO week.
    if (
        settings.weekly_review_enabled
        and now_local.weekday() == 0
        and not await _nudge_sent(
            session,
            user_id,
            NudgeKind.weekly_review,
            f"{now_local.isocalendar().year}-W{now_local.isocalendar().week:02d}",
        )
    ):
        session.add(
            NudgeDelivery(
                user_id=user_id,
                kind=NudgeKind.weekly_review,
                period_key=f"{now_local.isocalendar().year}-W{now_local.isocalendar().week:02d}",
                sent_at=now,
            )
        )
        await session.flush()
        await send(user_id, t(language, NUDGE_TEXT_KEYS[NudgeKind.weekly_review]))
        sent.append(NudgeKind.weekly_review)

    # Workout nudge: once per local day when the last workout is stale.
    if (
        settings.workout_nudge_enabled
        and not await _nudge_sent(
            session, user_id, NudgeKind.workout, local_date.isoformat()
        )
    ):
        last_workout = await session.scalar(
            select(WorkoutLog)
            .where(WorkoutLog.user_id == user_id)
            .order_by(WorkoutLog.started_at.desc(), WorkoutLog.id.desc())
            .limit(1)
        )
        if last_workout is None or (
            last_workout.started_at is not None
            and now - last_workout.started_at
            > timedelta(hours=WORKOUT_STALE_HOURS)
        ):
            session.add(
                NudgeDelivery(
                    user_id=user_id,
                    kind=NudgeKind.workout,
                    period_key=local_date.isoformat(),
                    sent_at=now,
                )
            )
            await session.flush()
            await send(user_id, t(language, NUDGE_TEXT_KEYS[NudgeKind.workout]))
            sent.append(NudgeKind.workout)

    return sent


async def expire_stale_actions(session: AsyncSession, now: datetime | None = None) -> int:
    """Mark proposed/confirmed pending actions past ``expires_at`` expired."""
    now = now or datetime.now(UTC)
    result = await session.execute(
        update(PendingAction)
        .where(
            PendingAction.status.in_(
                [
                    ActionStatus.proposed.value,
                    ActionStatus.confirmed.value,
                ]
            ),
            PendingAction.expires_at.is_not(None),
            PendingAction.expires_at < now,
        )
        .values(status=ActionStatus.expired.value, expired_at=now)
    )
    return result.rowcount or 0


async def run_proactive_pass(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    send=send_text,
) -> dict[str, int]:
    """One worker pass over all users plus pending-action expiry.

    Returns ``{"nudges_sent": N, "actions_expired": M}``. Each user is
    evaluated in its own short transaction so a send failure (e.g. bot API
    down) rolls back only that user's delivery rows and retries next pass.
    """
    now = now or datetime.now(UTC)
    users = (
        (
            await session.scalars(
                select(User)
                .options(
                    selectinload(User.settings),
                    selectinload(User.proactive_settings),
                )
                .order_by(User.id)
            )
        )
        .unique()
        .all()
    )
    # Pass only PKs to evaluate_user: commits/rollbacks below expire ORM
    # state, and a lazy attribute reload in async context would raise
    # MissingGreenlet.
    user_ids = [user.id for user in users]
    nudges_sent = 0
    for user_id in user_ids:
        try:
            kinds = await evaluate_user(session, user_id, now=now, send=send)
            await session.commit()
            nudges_sent += len(kinds)
        except Exception:
            await session.rollback()
            logger.warning(
                "proactive pass: failed for user %s, will retry next pass",
                user_id,
                exc_info=True,
            )
    expired = await expire_stale_actions(session, now=now)
    await session.commit()
    return {"nudges_sent": nudges_sent, "actions_expired": expired}
