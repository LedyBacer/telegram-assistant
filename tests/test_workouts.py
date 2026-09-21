"""Workout tracking tests against real PostgreSQL (SPEC §10)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.reminders import Reminder
from assistant.models.users import User
from assistant.models.workout_logs import WorkoutStatus
from assistant.services import workouts as wo
from assistant.services.users import upsert_user

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)  # a Tuesday


async def _user(session: AsyncSession, user_id: int = 21) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="W")
    user.settings.timezone = "UTC"
    await session.flush()
    return user


def _at(day: date) -> datetime:
    """A UTC time at 09:00 on the given date."""
    return datetime.combine(day, time(9, 0), tzinfo=UTC)


async def test_log_workout_defaults(session: AsyncSession) -> None:
    user = await _user(session)
    log = await wo.log_workout(session, user, name="Running", started_at=NOW)
    await session.commit()
    assert log.id is not None
    assert log.status == WorkoutStatus.completed.value
    assert log.duration_minutes is None
    assert log.perceived_effort is None


async def test_log_workout_validation(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ValueError, match="required"):
        await wo.log_workout(session, user, name="  ", started_at=NOW)
    with pytest.raises(ValueError, match="too long"):
        await wo.log_workout(
            session, user, name="x" * 201, started_at=NOW
        )
    with pytest.raises(ValueError, match="positive"):
        await wo.log_workout(
            session, user, name="X", started_at=NOW, duration_minutes=0
        )
    with pytest.raises(ValueError, match="1 and 10"):
        await wo.log_workout(
            session, user, name="X", started_at=NOW, perceived_effort=11
        )
    with pytest.raises(ValueError, match="too long"):
        await wo.log_workout(
            session, user, name="X", started_at=NOW, notes="x" * 4001
        )


async def test_log_workout_naive_time_uses_user_timezone(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    user.settings.timezone = "Europe/Berlin"  # +02:00 in September
    await session.flush()
    log = await wo.log_workout(
        session, user, name="Gym", started_at=datetime(2026, 9, 22, 9, 0)
    )
    assert log.started_at == datetime(2026, 9, 22, 7, 0, tzinfo=UTC)


async def test_get_and_list_workouts_respect_ownership(
    session: AsyncSession,
) -> None:
    user = await _user(session, user_id=21)
    other = await _user(session, user_id=22)
    mine = await wo.log_workout(session, user, name="Mine", started_at=NOW)
    theirs = await wo.log_workout(
        session, other, name="Theirs", started_at=NOW
    )
    await session.commit()

    assert (await wo.get_workout(session, user, mine.id)) is not None
    assert (await wo.get_workout(session, user, theirs.id)) is None
    assert (await wo.get_workout(session, user, 999999)) is None

    listed = await wo.list_workouts(session, user)
    assert [log.name for log in listed] == ["Mine"]
    listed = await wo.list_workouts(session, user, status=WorkoutStatus.completed)
    assert [log.name for log in listed] == ["Mine"]
    listed = await wo.list_workouts(
        session, user, status=WorkoutStatus.skipped
    )
    assert listed == []


async def test_workout_stats_streaks_and_week(session: AsyncSession) -> None:
    user = await _user(session)
    # The service computes streaks in the user's timezone (UTC here), so use
    # the UTC clock, not the host-local date.
    today = datetime.now(UTC).date()
    dates = [
        today,
        today - timedelta(days=1),
        today - timedelta(days=2),
        today - timedelta(days=10),  # gap: breaks the streak
    ]
    for i, day in enumerate(dates):
        await wo.log_workout(
            session, user, name=f"W{i}", started_at=_at(day),
            duration_minutes=30,
        )
    await wo.log_workout(
        session, user, name="Planned", started_at=_at(today + timedelta(days=1)),
        status=WorkoutStatus.planned,
    )
    await session.commit()

    stats = await wo.workout_stats(session, user)
    assert stats["total"] == 4  # planned excluded
    assert stats["total_minutes"] == 120
    week_start = today - timedelta(days=today.weekday())
    assert stats["this_week"] == sum(1 for d in dates if d >= week_start)
    assert stats["current_streak"] == 3
    assert stats["longest_streak"] == 3
    assert stats["last_date"] == today


async def test_workout_stats_empty(session: AsyncSession) -> None:
    user = await _user(session)
    stats = await wo.workout_stats(session, user)
    assert stats == {
        "total": 0,
        "total_minutes": 0,
        "this_week": 0,
        "current_streak": 0,
        "longest_streak": 0,
        "last_date": None,
    }


async def test_schedule_workout_creates_item_and_reminder(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    when = datetime(2026, 9, 23, 18, 0, tzinfo=UTC)
    item = await wo.schedule_workout(
        session, user, name="Running", starts_at=when, duration_minutes=45
    )
    await session.commit()
    assert item.title == "Workout: Running"
    assert item.starts_at == when
    assert item.source == "workout"
    assert item.extra.get("duration_minutes") == 45

    reminders = list(
        (
            await session.execute(
                select(Reminder).where(Reminder.calendar_item_id == item.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(reminders) == 1
    assert reminders[0].fire_at == when
