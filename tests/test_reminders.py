"""Durable reminder tests against real PostgreSQL (SPEC §8)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from assistant.models.calendar_items import CalendarItem
from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.models.reminders import Reminder, ReminderStatus
from assistant.models.users import User
from assistant.services import calendar as cal
from assistant.services import notifications
from assistant.services import reminders as rem
from assistant.services.jobs import create_job
from assistant.services.users import upsert_user

FIRE_AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_sender(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Reminder delivery tests must not call the Telegram API."""
    sender = AsyncMock()
    monkeypatch.setattr(notifications, "send_text", sender)
    return sender


async def _user(session: AsyncSession, user_id: int = 11) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="T")
    user.settings.timezone = "Europe/Berlin"
    await session.flush()
    return user


async def test_create_reminder_enqueues_durable_job(session: AsyncSession) -> None:
    user = await _user(session)
    reminder = await rem.create_reminder(
        session, user, fire_at=FIRE_AT, message="Take the meds"
    )
    await session.commit()
    assert reminder.status == ReminderStatus.pending.value
    assert reminder.job_id is not None
    job = await session.get(BackgroundJob, reminder.job_id)
    assert job.type == "reminder_send"
    assert job.status == JobStatus.pending.value
    assert job.available_at == FIRE_AT
    assert job.idempotency_key == f"reminder:{reminder.id}"


async def test_create_reminder_validation(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ValueError, match="required"):
        await rem.create_reminder(session, user, fire_at=FIRE_AT, message="  ")
    with pytest.raises(ValueError, match="too long"):
        await rem.create_reminder(
            session, user, fire_at=FIRE_AT, message="x" * 1001
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        await rem.create_reminder(
            session, user, fire_at=datetime(2026, 10, 1, 12, 0), message="x"
        )


async def test_create_reminder_rejects_foreign_item(
    session: AsyncSession,
) -> None:
    user = await _user(session, user_id=11)
    other = await _user(session, user_id=12)
    item = await cal.create_item(
        session, other, title="Other's", starts_at=FIRE_AT
    )
    await session.commit()
    with pytest.raises(ValueError, match="another user"):
        await rem.create_reminder(
            session, user, fire_at=FIRE_AT, message="x", calendar_item=item
        )


async def test_create_item_reminders_resolves_offsets(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    item = await cal.create_item(
        session, user, title="Standup", starts_at=datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    )
    reminders = await rem.create_item_reminders(
        session, user, item, offsets_minutes=[30, 0]
    )
    await session.commit()
    assert [r.fire_at for r in reminders] == [
        datetime(2026, 10, 1, 8, 30, tzinfo=UTC),
        datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
    ]
    for r in reminders:
        assert r.trigger_type == "item_linked"
        assert r.calendar_item_id == item.id
        # The stored text is the user's title only; the localized
        # "Reminder:" wrapper is applied at delivery time.
        assert r.message == "Standup"


async def test_create_item_reminders_without_starts_at_is_empty(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="All-day-ish")
    assert await rem.create_item_reminders(
        session, user, item, offsets_minutes=[0]
    ) == []


async def test_cancel_reminder_cancels_job(session: AsyncSession) -> None:
    user = await _user(session)
    reminder = await rem.create_reminder(
        session, user, fire_at=FIRE_AT, message="x"
    )
    await session.commit()
    cancelled = await rem.cancel_reminder(session, user, reminder.id)
    await session.commit()
    assert cancelled is not None
    assert cancelled.status == ReminderStatus.cancelled.value
    assert cancelled.cancelled_at is not None
    job = await session.get(BackgroundJob, reminder.job_id)
    assert job.status == JobStatus.cancelled.value
    # Cancelling again is a no-op.
    again = await rem.cancel_reminder(session, user, reminder.id)
    assert again.status == ReminderStatus.cancelled.value


async def test_cancel_reminder_is_user_scoped(session: AsyncSession) -> None:
    user = await _user(session, user_id=11)
    other = await _user(session, user_id=12)
    reminder = await rem.create_reminder(
        session, user, fire_at=FIRE_AT, message="x"
    )
    await session.commit()
    assert await rem.get_reminder(session, other, reminder.id) is None
    assert await rem.cancel_reminder(session, other, reminder.id) is None


async def test_cancel_item_reminders_only_pending(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(
        session, user, title="Meeting", starts_at=datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
    )
    reminders = await rem.create_item_reminders(
        session, user, item, offsets_minutes=[30, 0]
    )
    await session.commit()
    # Mark one as already sent.
    sent_job = await session.get(BackgroundJob, reminders[0].job_id)
    sent_job.status = JobStatus.completed.value
    reminders[0].status = ReminderStatus.sent.value
    await session.commit()

    count = await rem.cancel_item_reminders(session, user, item.id)
    await session.commit()
    assert count == 1
    assert reminders[0].status == ReminderStatus.sent.value
    assert reminders[1].status == ReminderStatus.cancelled.value


async def _run_handler(session: AsyncSession, job: BackgroundJob) -> None:
    from assistant.worker import registry

    handler = registry.handlers["reminder_send"]
    job.status = JobStatus.running.value
    job.locked_by = "test-worker"
    # V5 §3: the handler now re-validates DB ownership before sending, so the
    # simulated job must carry a live lease (locked_by + future lease_until).
    job.lease_until = datetime.now(UTC) + timedelta(minutes=5)
    await session.flush()
    await handler(session, job)
    await session.flush()


async def test_handler_marks_sent_exactly_once(session: AsyncSession) -> None:
    user = await _user(session)
    reminder = await rem.create_reminder(
        session, user, fire_at=FIRE_AT, message="x"
    )
    job = await session.get(BackgroundJob, reminder.job_id)
    await session.commit()

    await _run_handler(session, job)
    await session.commit()
    assert reminder.status == ReminderStatus.sent.value
    first_sent_at = reminder.sent_at
    assert first_sent_at is not None

    # Simulate a retry: handler runs again, must not change state.
    await _run_handler(session, job)
    await session.commit()
    assert reminder.status == ReminderStatus.sent.value
    assert reminder.sent_at == first_sent_at


async def test_handler_ignores_cancelled_reminder(session: AsyncSession) -> None:
    user = await _user(session)
    reminder = await rem.create_reminder(
        session, user, fire_at=FIRE_AT, message="x"
    )
    job = await session.get(BackgroundJob, reminder.job_id)
    await session.commit()
    await rem.cancel_reminder(session, user, reminder.id)
    await session.commit()

    await _run_handler(session, job)
    await session.commit()
    assert reminder.status == ReminderStatus.cancelled.value
    assert reminder.sent_at is None


async def test_handler_missing_reminder_is_noop(session: AsyncSession) -> None:
    user = await _user(session)
    job = await create_job(
        session,
        type="reminder_send",
        payload={"reminder_id": 999999},
        user_id=user.id,
    )
    await session.commit()
    await _run_handler(session, job)
    await session.commit()  # must not raise


async def test_handler_missing_payload_raises(session: AsyncSession) -> None:
    user = await _user(session)
    job = await create_job(
        session, type="reminder_send", payload={}, user_id=user.id
    )
    await session.commit()
    with pytest.raises(ValueError, match="reminder_id"):
        await _run_handler(session, job)
    await session.rollback()


async def test_reminder_send_handler_registered() -> None:
    import assistant.worker.handlers  # noqa: F401
    from assistant.worker import registry

    assert "reminder_send" in registry.handlers


async def test_list_reminders_orders_by_fire_at(session: AsyncSession) -> None:
    user = await _user(session)
    r1 = await rem.create_reminder(session, user, fire_at=FIRE_AT, message="b")
    r2 = await rem.create_reminder(
        session, user, fire_at=FIRE_AT - timedelta(hours=1), message="a"
    )
    await session.commit()
    all_rems = await rem.list_reminders(session, user)
    assert [r.id for r in all_rems] == [r2.id, r1.id]
    pending = await rem.list_reminders(session, user, status=ReminderStatus.pending)
    assert {r.id for r in pending} == {r1.id, r2.id}


# ---------------------------------------------------------------------------
# Domain hardening (P14): dedupe + deterministic ordering
# ---------------------------------------------------------------------------


async def test_create_item_reminders_dedupes_offsets(session: AsyncSession) -> None:
    user = await _user(session)
    item = await cal.create_item(
        session,
        user,
        title="Meeting",
        starts_at=FIRE_AT,
    )
    await session.commit()
    created = await rem.create_item_reminders(
        session, user, item, offsets_minutes=[0, 0, 30]
    )
    await session.commit()
    assert len(created) == 2
    assert sorted(r.offset_minutes for r in created) == [0, 30]


# ---------------------------------------------------------------------------
# SPEC §14.2: shared offset validation (V3 P31)
# ---------------------------------------------------------------------------


def test_validate_reminder_offsets_dedupes_and_bounded() -> None:
    assert rem.validate_reminder_offsets([30, 0, 30]) == [30, 0]
    # Bounds are inclusive at ±1 day.
    assert rem.validate_reminder_offsets([0, 1440, -1440]) == [0, 1440, -1440]


@pytest.mark.parametrize(
    ("offsets", "match"),
    [
        ([-1441], "between"),
        ([1441], "between"),
        (list(range(6)), "At most"),
        (["30"], "integer"),
    ],
)
def test_validate_reminder_offsets_rejects(offsets: list, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        rem.validate_reminder_offsets(offsets)


async def test_create_item_reminders_enforces_max(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    item = await cal.create_item(session, user, title="Busy", starts_at=FIRE_AT)
    await session.commit()
    with pytest.raises(ValueError, match="At most"):
        await rem.create_item_reminders(
            session, user, item, offsets_minutes=list(range(6))
        )


async def test_list_reminders_tiebreak_by_id(session: AsyncSession) -> None:
    user = await _user(session)
    r1 = await rem.create_reminder(session, user, fire_at=FIRE_AT, message="one")
    r2 = await rem.create_reminder(session, user, fire_at=FIRE_AT, message="two")
    await session.commit()
    rows = await rem.list_reminders(session, user)
    assert [r.id for r in rows] == sorted([r1.id, r2.id])


async def test_create_item_reminders_concurrent_same_offsets(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    """V5 §7: two concurrent creates of the SAME offsets on one item must not
    duplicate an offset. The item-row ``FOR UPDATE`` lock serializes the two
    creates; the second dedupes against the first's committed offsets, so
    exactly one create inserts the full set and the other inserts none."""
    user = await _user(session)
    item = await cal.create_item(
        session, user, title="Standup", starts_at=FIRE_AT
    )
    await session.commit()
    uid, iid = user.id, item.id

    offsets = [0, 15, 30, 60, 120]  # exactly MAX_REMINDERS_PER_ITEM

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def create_in_new_session() -> int:
        async with factory() as s2:
            u = await s2.get(User, uid)
            it = await s2.get(CalendarItem, iid)
            created = await rem.create_item_reminders(
                s2, u, it, offsets_minutes=list(offsets)
            )
            await s2.commit()
            return len(created)

    results = await asyncio.gather(
        create_in_new_session(), create_in_new_session()
    )
    # One create inserted all five; the other deduped to zero (no cap breach:
    # the union of identical offsets stays at five).
    assert sorted(results) == [0, 5]

    rows = (
        await session.scalars(
            select(Reminder).where(
                Reminder.calendar_item_id == iid,
                Reminder.status == ReminderStatus.pending.value,
                Reminder.trigger_type == "item_linked",
            )
        )
    ).all()
    assert len(rows) == len(offsets)
    assert sorted(r.offset_minutes for r in rows) == sorted(offsets)
