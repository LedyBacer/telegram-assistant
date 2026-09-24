"""Workout logging, history, statistics, and scheduling (SPEC §10).

Timestamps are stored as UTC; naive inputs are interpreted in the user's
timezone. Streaks and "this week" statistics are computed in the user's
local timezone. No medical or diagnostic functionality (SPEC §10).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.i18n import LocalizableError
from assistant.models.calendar_items import CalendarItem, ItemKind
from assistant.models.users import User
from assistant.models.workout_logs import WorkoutLog, WorkoutStatus
from assistant.services import calendar as calendar_service
from assistant.services import reminders as reminders_service

_DEFAULT_LIST_LIMIT = 20


def _user_tz(user: User) -> ZoneInfo:
    name = user.settings.timezone if user.settings is not None else "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError):
        return ZoneInfo("UTC")


def _to_utc(value: datetime | None, tz: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(UTC)


async def log_workout(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    started_at: datetime | None = None,
    duration_minutes: int | None = None,
    notes: str | None = None,
    perceived_effort: int | None = None,
    status: WorkoutStatus = WorkoutStatus.completed,
) -> WorkoutLog:
    """Record a workout. Naive ``started_at`` is taken in the user's timezone."""
    if not name.strip():
        raise LocalizableError("workouts.err_name")
    if len(name) > 200:
        raise LocalizableError("workouts.err_name_long")
    if duration_minutes is not None and duration_minutes <= 0:
        raise LocalizableError("workouts.err_duration")
    if notes and len(notes) > 4000:
        raise LocalizableError("workouts.err_notes", max=4000)
    if perceived_effort is not None and not 1 <= perceived_effort <= 10:
        raise LocalizableError("workouts.err_effort")

    tz = _user_tz(user)
    log = WorkoutLog(
        user_id=user.id,
        name=name.strip(),
        started_at=_to_utc(started_at, tz) or datetime.now(UTC),
        duration_minutes=duration_minutes,
        notes=notes,
        perceived_effort=perceived_effort,
        status=status.value,
    )
    session.add(log)
    await session.flush()
    return log


async def get_workout(
    session: AsyncSession, user: User, workout_id: int
) -> WorkoutLog | None:
    log = await session.get(WorkoutLog, workout_id)
    if log is None or log.user_id != user.id:
        return None
    return log


async def list_workouts(
    session: AsyncSession,
    user: User,
    *,
    limit: int = _DEFAULT_LIST_LIMIT,
    status: WorkoutStatus | None = None,
) -> list[WorkoutLog]:
    """List the user's workouts, most recent first."""
    stmt = (
        select(WorkoutLog)
        .where(WorkoutLog.user_id == user.id)
        .order_by(WorkoutLog.started_at.desc(), WorkoutLog.id.desc())
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(WorkoutLog.status == status.value)
    return list((await session.scalars(stmt)).all())


async def workout_stats(
    session: AsyncSession, user: User
) -> dict:
    """Simple statistics and streaks over completed workouts (SPEC §10).

    Returns ``total``, ``total_minutes``, ``this_week``, ``current_streak``,
    ``longest_streak`` (days), and ``last_date`` (user-local date or None).
    """
    logs = (
        (
            await session.execute(
                select(WorkoutLog).where(
                    WorkoutLog.user_id == user.id,
                    WorkoutLog.status == WorkoutStatus.completed.value,
                )
                .order_by(WorkoutLog.started_at)
            )
        )
        .scalars()
        .all()
    )
    if not logs:
        return {
            "total": 0,
            "total_minutes": 0,
            "this_week": 0,
            "current_streak": 0,
            "longest_streak": 0,
            "last_date": None,
        }

    tz = _user_tz(user)
    today = datetime.now(tz=tz).date()
    local_dates = sorted({log.started_at.astimezone(tz).date() for log in logs})
    total_minutes = sum(log.duration_minutes or 0 for log in logs)
    week_start = today - timedelta(days=today.weekday())
    this_week = sum(1 for d in local_dates if d >= week_start)

    # Current streak: consecutive days ending today (or yesterday, so the
    # streak is not broken before the day is over).
    date_set = set(local_dates)
    current = 0
    cursor = today if today in date_set else today - timedelta(days=1)
    while cursor in date_set:
        current += 1
        cursor -= timedelta(days=1)

    longest = 1
    run = 1
    for prev, cur in zip(local_dates, local_dates[1:], strict=False):
        run = run + 1 if cur - prev == timedelta(days=1) else 1
        longest = max(longest, run)

    return {
        "total": len(logs),
        "total_minutes": total_minutes,
        "this_week": this_week,
        "current_streak": current,
        "longest_streak": longest,
        "last_date": local_dates[-1],
    }


async def schedule_workout(
    session: AsyncSession,
    user: User,
    *,
    name: str,
    starts_at: datetime,
    duration_minutes: int | None = None,
    ends_at: datetime | None = None,
) -> CalendarItem:
    """Create a workout calendar item plus a reminder at its start time.

    The item's ``ends_at`` is an explicit ``ends_at`` when given, otherwise
    derived from ``starts_at`` + ``duration_minutes``. Both are normalized to
    UTC in the user's timezone before being handed to the calendar service.
    """
    tz = _user_tz(user)
    if ends_at is not None:
        effective_end: datetime | None = _to_utc(ends_at, tz)
    elif duration_minutes is not None:
        effective_end = _to_utc(starts_at, tz) + timedelta(minutes=duration_minutes)
    else:
        effective_end = None
    item = await calendar_service.create_item(
        session,
        user,
        # V5 §6.1: persist the raw workout name — the "workout" identity is
        # carried by ``source="workout"``, not a title prefix, so the Mini
        # App / calendar can render a distinct workout visual.
        title=name.strip(),
        kind=ItemKind.task,
        starts_at=starts_at,
        ends_at=effective_end,
        source="workout",
        extra={"duration_minutes": duration_minutes} if duration_minutes else {},
    )
    await reminders_service.create_item_reminders(
        session, user, item, offsets_minutes=[0]
    )
    return item
