"""Limited proactivity pass (worker-driven, deterministic, anti-spam gated).

Triggers are derived from application state only (no model calls):

- ``weekly_review``: on Monday (user's local time), once per ISO week. The
  message is a deterministic summary of the user's actual state (overdue
  count, items completed this week, upcoming high-priority items, workout
  totals). A week with nothing to report produces no nudge.
- ``workout``: once per user-local day when the last completed workout is
  older than :data:`WORKOUT_STALE_HOURS` (or there is none).
- ``overdue``: once per user-local day when at least one scheduled item is
  past its due date.

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
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from assistant.i18n import DEFAULT_LANGUAGE, t
from assistant.models.calendar_items import CalendarItem, ItemPriority, ItemStatus
from assistant.models.pending_actions import ActionStatus, PendingAction
from assistant.models.proactivity import NudgeDelivery, NudgeKind, ProactiveSettings
from assistant.models.users import User, UserSettings
from assistant.models.workout_logs import WorkoutLog
from assistant.services.notifications import send_text

logger = logging.getLogger("assistant.proactivity")

#: A workout is "stale" once it is older than this (drives the workout nudge).
WORKOUT_STALE_HOURS = 48

#: The weekly review lists high-priority items starting within this window.
UPCOMING_WINDOW_DAYS = 7
#: At most this many upcoming titles make it into the summary.
UPCOMING_LIMIT = 3

NUDGE_TEXT_KEYS = {
    NudgeKind.workout: "proactive.workout",
    NudgeKind.overdue: "proactive.overdue",
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


async def _reserve_nudge(
    session: AsyncSession,
    user_id: int,
    kind: str,
    period_key: str,
    sent_at: datetime,
) -> bool:
    """Atomically claim the (user, kind, period) slot; True when claimed.

    ``INSERT ... ON CONFLICT DO NOTHING`` on the unique constraint IS the
    reservation: with any number of concurrent worker passes exactly one of
    them inserts the row, so there is no check-then-insert race (V3 P40).
    """
    result = await session.execute(
        insert(NudgeDelivery)
        .values(user_id=user_id, kind=kind, period_key=period_key, sent_at=sent_at)
        .on_conflict_do_nothing(index_elements=["user_id", "kind", "period_key"])
    )
    return (result.rowcount or 0) == 1


def _overdue_expr(now: datetime):
    """Scheduled items whose anchor (due_at, falling back to starts_at) is past."""
    return func.coalesce(CalendarItem.due_at, CalendarItem.starts_at) < now


async def _overdue_count(
    session: AsyncSession, user_id: int, now: datetime
) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(CalendarItem)
            .where(
                CalendarItem.user_id == user_id,
                CalendarItem.status == ItemStatus.scheduled.value,
                _overdue_expr(now),
            )
        )
        or 0
    )


async def _weekly_review_stats(
    session: AsyncSession,
    user_id: int,
    now: datetime,
    week_start_utc: datetime,
) -> dict:
    """Deterministic state for the weekly review (counts, titles, minutes)."""
    upcoming = [
        row[0]
        for row in (
            await session.execute(
                select(CalendarItem.title)
                .where(
                    CalendarItem.user_id == user_id,
                    CalendarItem.status == ItemStatus.scheduled.value,
                    CalendarItem.priority == ItemPriority.high.value,
                    CalendarItem.starts_at.is_not(None),
                    CalendarItem.starts_at >= now,
                    CalendarItem.starts_at
                    < now + timedelta(days=UPCOMING_WINDOW_DAYS),
                )
                .order_by(CalendarItem.starts_at)
                .limit(UPCOMING_LIMIT)
            )
        ).all()
    ]
    workouts, workout_minutes = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(WorkoutLog.duration_minutes), 0),
            ).where(
                WorkoutLog.user_id == user_id,
                WorkoutLog.started_at >= week_start_utc,
            )
        )
    ).one()
    completed = (
        await session.scalar(
            select(func.count())
            .select_from(CalendarItem)
            .where(
                CalendarItem.user_id == user_id,
                CalendarItem.status == ItemStatus.completed.value,
                CalendarItem.completed_at >= week_start_utc,
            )
        )
        or 0
    )
    return {
        "overdue": await _overdue_count(session, user_id, now),
        "completed": completed,
        "upcoming": upcoming,
        "workouts": workouts,
        "workout_minutes": workout_minutes,
    }


def _weekly_review_text(language: str, stats: dict) -> str:
    """Compose the weekly review from the deterministic stats (no LLM)."""
    lines: list[str] = []
    if stats["overdue"]:
        lines.append(t(language, "proactive.wr_overdue", n=stats["overdue"]))
    if stats["completed"]:
        lines.append(t(language, "proactive.wr_completed", n=stats["completed"]))
    if stats["upcoming"]:
        lines.append(
            t(language, "proactive.wr_upcoming", items="; ".join(stats["upcoming"]))
        )
    if stats["workouts"]:
        lines.append(
            t(
                language,
                "proactive.wr_workouts",
                n=stats["workouts"],
                minutes=stats["workout_minutes"],
            )
        )
    body = "\n".join(f"• {line}" for line in lines)
    return f"{t(language, 'proactive.weekly_review')}\n{body}"


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

    Delivery semantics: **at-most-once**. Each ``NudgeDelivery`` dedupe row
    is committed BEFORE the send, so a failed send (bot API down, blocked
    chat) loses that nudge instead of re-sending it on the next pass — a
    nudge is a convenience, not a commitment (contrast the durable
    at-least-once reminders/digests).

    Concurrency (V3 P40): concurrent worker passes for the same user are
    serialized by a FOR UPDATE lock on the settings row plus an atomic
    ``ON CONFLICT DO NOTHING`` reservation of the delivery slot, so the
    same nudge is never sent twice even across sessions.

    Returns the list of nudge kinds actually sent this call.
    """
    now = now or datetime.now(UTC)
    user_settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    # FOR UPDATE serializes concurrent worker passes for this user (V3 P40):
    # the gate counters below are read under the row lock, and the nudge
    # reservation is an atomic upsert, so two sessions can never both send
    # the same nudge. A first-run race on the settings row itself (both
    # create defaults) surfaces as an IntegrityError, which the pass treats
    # as a normal per-user failure and retries next pass.
    settings = await session.scalar(
        select(ProactiveSettings)
        .where(ProactiveSettings.user_id == user_id)
        .with_for_update()
    )
    if settings is None:
        settings = ProactiveSettings(user_id=user_id)
        session.add(settings)
        await session.flush()
    if not settings.enabled:
        return []

    tz = _user_tz(user_settings)
    now_local = now.astimezone(tz)
    local_date = now_local.date()
    # Convert the local day's start/end back to UTC for the counter query.
    day_start_utc = datetime.combine(local_date, time.min, tzinfo=tz).astimezone(UTC)
    day_end_utc = day_start_utc + timedelta(days=1)
    language = _user_lang(user_settings)
    week_key = f"{now_local.isocalendar().year}-W{now_local.isocalendar().week:02d}"
    # The week starts at local MIDNIGHT on Monday (not "now minus 6 days"):
    # a Monday-morning review must already include what was done that morning.
    week_start_utc = (
        now_local - timedelta(days=now_local.weekday())
    ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    async def gates_open() -> bool:
        """Re-run ALL anti-spam gates against the CURRENT delivery state.

        Called before EVERY nudge reservation (V4 §9-11). Because a nudge
        committed earlier in this pass advances the daily count and the
        last-sent timestamp, the just-sent nudge suppresses every lower-
        priority nudge in the same pass — the cross-kind anti-spam the old
        once-at-the-top gate read failed to enforce.
        """
        if _in_quiet_hours(now_local, settings):
            return False
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
            return False
        last = await session.scalar(
            select(func.max(NudgeDelivery.sent_at)).where(
                NudgeDelivery.user_id == user_id
            )
        )
        return last is None or now - last >= timedelta(
            minutes=settings.min_interval_minutes
        )

    sent: list[str] = []
    # Deterministic priority (V4 §10): weekly review > overdue > workout.

    # 1) Weekly review: Monday in the user's local time, once per ISO week.
    # The message is a deterministic summary of the user's actual state; a
    # week with nothing to report produces no nudge.
    weekly_stats: dict | None = None
    if (
        settings.weekly_review_enabled
        and now_local.weekday() == 0
        and await gates_open()
    ):
        weekly_stats = await _weekly_review_stats(
            session, user_id, now, week_start_utc
        )
        has_content = (
            weekly_stats["overdue"]
            or weekly_stats["completed"]
            or weekly_stats["upcoming"]
            or weekly_stats["workouts"]
        )
        if has_content and await _reserve_nudge(
            session, user_id, NudgeKind.weekly_review, week_key, now
        ):
            # Commit the dedupe row BEFORE the send (at-most-once): if the
            # send fails, this nudge is lost, not re-sent next pass.
            await session.commit()
            await send(user_id, _weekly_review_text(language, weekly_stats))
            sent.append(NudgeKind.weekly_review)

    # 2) Overdue: once per local day when something is past due.
    if settings.overdue_nudge_enabled and await gates_open():
        overdue_count = (
            weekly_stats["overdue"]
            if weekly_stats is not None
            else await _overdue_count(session, user_id, now)
        )
        if overdue_count and await _reserve_nudge(
            session, user_id, NudgeKind.overdue, local_date.isoformat(), now
        ):
            # Commit the dedupe row BEFORE the send (at-most-once).
            await session.commit()
            await send(
                user_id, t(language, NUDGE_TEXT_KEYS[NudgeKind.overdue], n=overdue_count)
            )
            sent.append(NudgeKind.overdue)

    # 3) Workout: once per local day when the last workout is stale and there
    # is no scheduled workout left on the calendar (today or later): a plan
    # already on the books is the user's own answer (V3 P39).
    if settings.workout_nudge_enabled and await gates_open():
        last_workout = await session.scalar(
            select(WorkoutLog)
            .where(WorkoutLog.user_id == user_id)
            .order_by(WorkoutLog.started_at.desc(), WorkoutLog.id.desc())
            .limit(1)
        )
        stale = last_workout is None or (
            last_workout.started_at is not None
            and now - last_workout.started_at > timedelta(hours=WORKOUT_STALE_HOURS)
        )
        planned = (
            await session.scalar(
                select(func.count())
                .select_from(CalendarItem)
                .where(
                    CalendarItem.user_id == user_id,
                    CalendarItem.status == ItemStatus.scheduled.value,
                    CalendarItem.source == "workout",
                    CalendarItem.starts_at.is_not(None),
                    CalendarItem.starts_at >= day_start_utc,
                )
            )
            or 0
        )
        if stale and not planned and await _reserve_nudge(
            session, user_id, NudgeKind.workout, local_date.isoformat(), now
        ):
            # Commit the dedupe row BEFORE the send (at-most-once).
            await session.commit()
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
    down) affects only that user. Nudge delivery is at-most-once: the
    dedupe rows are committed before the sends, so a failed send is NOT
    retried on the next pass (the nudge is lost, not duplicated).
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
