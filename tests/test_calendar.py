"""Calendar service tests against real PostgreSQL (SPEC §7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.calendar_items import ItemKind, ItemPriority, ItemStatus
from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.models.reminders import Reminder, ReminderStatus
from assistant.models.users import User
from assistant.services import calendar as cal
from assistant.services import reminders as rem
from assistant.services.users import upsert_user

BERLIN = ZoneInfo("Europe/Berlin")


async def _user(session: AsyncSession, user_id: int = 11) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="T")
    user.settings.timezone = "Europe/Berlin"
    await session.flush()
    return user


async def test_create_item_normalizes_naive_to_utc(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(
        session,
        user,
        title="Call doctor",
        starts_at=datetime(2026, 9, 22, 18, 30),  # naive Berlin
    )
    await session.commit()
    assert item.starts_at == datetime(2026, 9, 22, 16, 30, tzinfo=UTC)
    assert item.status == ItemStatus.scheduled.value
    assert item.kind == ItemKind.task.value


async def test_create_item_validation(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ValueError, match="required"):
        await cal.create_item(session, user, title="   ")
    with pytest.raises(ValueError, match="too long"):
        await cal.create_item(session, user, title="x" * 501)


async def test_get_item_is_user_scoped(session: AsyncSession) -> None:
    user = await _user(session, user_id=11)
    other = await _user(session, user_id=12)
    item = await cal.create_item(session, user, title="Private")
    await session.commit()
    assert await cal.get_item(session, user, item.id) is not None
    assert await cal.get_item(session, other, item.id) is None


async def test_complete_item(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="Done soon")
    await session.commit()
    completed = await cal.complete_item(session, user, item.id)
    await session.commit()
    assert completed is not None
    assert completed.status == ItemStatus.completed.value
    assert completed.completed_at is not None
    assert completed not in await cal.list_today(session, user)


async def test_cancel_item_cancels_pending_reminders(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(
        session,
        user,
        title="Meeting",
        starts_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    created = await rem.create_item_reminders(session, user, item, offsets_minutes=[30, 0])
    await session.commit()
    assert len(created) == 2

    cancelled = await cal.cancel_item(session, user, item.id)
    await session.commit()
    assert cancelled is not None
    assert cancelled.status == ItemStatus.cancelled.value
    for r in created:
        assert r.status == ReminderStatus.cancelled.value
        assert r.cancelled_at is not None
        job = await session.get(BackgroundJob, r.job_id)
        assert job.status == JobStatus.cancelled.value


async def test_delete_item_cancels_jobs_and_removes_reminders(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    item = await cal.create_item(
        session,
        user,
        title="To delete",
        starts_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    reminders = await rem.create_item_reminders(
        session, user, item, offsets_minutes=[0]
    )
    job_id = reminders[0].job_id
    reminder_id = reminders[0].id
    await session.commit()
    assert await cal.delete_item(session, user, item.id) is True
    await session.commit()
    assert await session.get(Reminder, reminder_id) is None
    job = await session.get(BackgroundJob, job_id)
    assert job.status == JobStatus.cancelled.value
    assert await cal.delete_item(session, user, item.id) is False


async def test_list_today_and_upcoming(session: AsyncSession) -> None:
    user = await _user(session)
    now_local = datetime.now(BERLIN)
    today_item = await cal.create_item(
        session,
        user,
        title="Today",
        starts_at=now_local.replace(hour=12, minute=0, second=0, microsecond=0)
        .astimezone(UTC),
    )
    tomorrow_item = await cal.create_item(
        session,
        user,
        title="Tomorrow",
        starts_at=(now_local.replace(hour=12, minute=0, second=0, microsecond=0)
                   + timedelta(days=1)).astimezone(UTC),
    )
    await session.commit()

    today = await cal.list_today(session, user)
    assert [i.id for i in today] == [today_item.id]

    upcoming = await cal.list_upcoming(session, user)
    assert tomorrow_item.id in [i.id for i in upcoming]

    start = (now_local - timedelta(days=1)).astimezone(UTC)
    end = (now_local + timedelta(days=2)).astimezone(UTC)
    ranged = await cal.list_range(session, user, start=start, end=end)
    assert {i.id for i in ranged} == {today_item.id, tomorrow_item.id}


async def test_list_range_includes_completed(session: AsyncSession) -> None:
    """The Mini App month view keeps completed items on their day."""
    user = await _user(session)
    now_local = datetime.now(BERLIN)
    item = await cal.create_item(
        session,
        user,
        title="Done",
        starts_at=now_local.replace(hour=9, minute=0, second=0, microsecond=0)
        .astimezone(UTC),
    )
    await cal.complete_item(session, user, item.id)
    await session.commit()

    start = (now_local - timedelta(days=1)).astimezone(UTC)
    end = (now_local + timedelta(days=1)).astimezone(UTC)
    ranged = await cal.list_range(session, user, start=start, end=end)
    assert [i.id for i in ranged] == [item.id]
    assert ranged[0].status == ItemStatus.completed.value

    # The scheduled-only listing still excludes it.
    scheduled = await cal.list_items(session, user, start=start, end=end)
    assert scheduled == []


async def test_list_range_validation(session: AsyncSession) -> None:
    user = await _user(session)
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="after start"):
        await cal.list_range(session, user, start=now, end=now)


async def test_update_item(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="Old")
    await session.commit()
    updated = await cal.update_item(
        session, user, item.id, title="New", priority=ItemPriority.high
    )
    await session.commit()
    assert updated is not None
    assert updated.title == "New"
    assert updated.priority == ItemPriority.high.value
    with pytest.raises(ValueError, match="empty"):
        await cal.update_item(session, user, item.id, title="  ")


async def test_update_item_tri_state(session: AsyncSession) -> None:
    """SPEC §4.3: omitted = unchanged, explicit None = clear, value = set."""
    user = await _user(session)
    item = await cal.create_item(
        session,
        user,
        title="Tri",
        description="desc",
        starts_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        ends_at=datetime(2026, 10, 1, 13, 0, tzinfo=UTC),
        due_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
    )
    await session.commit()

    # Explicit None clears; every omitted field keeps its value.
    updated = await cal.update_item(session, user, item.id, ends_at=None)
    await session.commit()
    assert updated.ends_at is None
    assert updated.starts_at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert updated.due_at == datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
    assert updated.description == "desc"

    # Explicit value sets again.
    updated = await cal.update_item(
        session, user, item.id, ends_at=datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
    )
    await session.commit()
    assert updated.ends_at == datetime(2026, 10, 1, 14, 0, tzinfo=UTC)

    # Description can also be cleared.
    updated = await cal.update_item(session, user, item.id, description=None)
    await session.commit()
    assert updated.description is None


async def test_complete_item_cancels_pending_reminders(session: AsyncSession) -> None:
    """SPEC §4.2: completing an item handles its reminders via the shared
    service, from every surface."""
    user = await _user(session)
    item = await cal.create_item(
        session,
        user,
        title="Wrap-up",
        starts_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    created = await rem.create_item_reminders(session, user, item, offsets_minutes=[0])
    await session.commit()

    completed = await cal.complete_item(session, user, item.id)
    await session.commit()
    assert completed is not None
    assert completed.status == ItemStatus.completed.value
    assert created[0].status == ReminderStatus.cancelled.value
    assert created[0].cancelled_at is not None
    job = await session.get(BackgroundJob, created[0].job_id)
    assert job.status == JobStatus.cancelled.value


async def test_reschedule_recomputes_linked_reminders(session: AsyncSession) -> None:
    """SPEC §4.2: moving an item's start moves its linked reminders (fire_at
    and the delivery job's availability) with it."""
    user = await _user(session)
    item = await cal.create_item(
        session,
        user,
        title="Meeting",
        starts_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    created = await rem.create_item_reminders(session, user, item, offsets_minutes=[30, 0])
    await session.commit()
    assert created[0].fire_at == datetime(2026, 10, 1, 11, 30, tzinfo=UTC)

    updated = await cal.update_item(
        session, user, item.id, starts_at=datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
    )
    await session.commit()
    assert updated.starts_at == datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
    for r in created:
        assert r.status == ReminderStatus.pending.value
        expected = datetime(2026, 10, 1, 14, 0, tzinfo=UTC) - timedelta(
            minutes=r.offset_minutes
        )
        assert r.fire_at == expected
        job = await session.get(BackgroundJob, r.job_id)
        assert job.status == JobStatus.pending.value
        assert job.available_at == expected

    # A title rename refreshes the reminder text too.
    updated = await cal.update_item(session, user, item.id, title="Big meeting")
    await session.commit()
    assert all(r.message == "Big meeting" for r in created)


async def test_clearing_start_cancels_linked_reminders(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(
        session,
        user,
        title="Unanchored",
        starts_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    created = await rem.create_item_reminders(session, user, item, offsets_minutes=[0])
    await session.commit()

    updated = await cal.update_item(session, user, item.id, starts_at=None)
    await session.commit()
    assert updated.starts_at is None
    assert created[0].status == ReminderStatus.cancelled.value
    job = await session.get(BackgroundJob, created[0].job_id)
    assert job.status == JobStatus.cancelled.value


# ---------------------------------------------------------------------------
# Domain hardening (P14): temporal invariants + deterministic ordering
# ---------------------------------------------------------------------------


async def test_create_item_rejects_end_before_start(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ValueError, match="ends_at"):
        await cal.create_item(
            session,
            user,
            title="Bad range",
            starts_at=datetime(2026, 10, 1, 14, 0, tzinfo=UTC),
            ends_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        )


async def test_update_item_rejects_end_before_start(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(
        session,
        user,
        title="Range",
        starts_at=datetime(2026, 10, 1, 14, 0, tzinfo=UTC),
        ends_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
    )
    await session.commit()
    with pytest.raises(ValueError, match="ends_at"):
        await cal.update_item(
            session,
            user,
            item.id,
            ends_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        )
    await session.commit()


async def test_list_items_tiebreak_by_id(session: AsyncSession) -> None:
    user = await _user(session)
    same_start = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    a = await cal.create_item(session, user, title="A", starts_at=same_start)
    b = await cal.create_item(session, user, title="B", starts_at=same_start)
    await session.commit()
    items = await cal.list_items(
        session,
        user,
        start=datetime(2026, 10, 1, 0, 0, tzinfo=UTC),
        end=datetime(2026, 10, 2, 0, 0, tzinfo=UTC),
    )
    assert [i.id for i in items] == sorted([a.id, b.id])
