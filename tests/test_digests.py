"""Morning digest + motivation + reminder delivery tests (SPEC §16-17) —
real PostgreSQL, fake Telegram sender."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.calendar_items import ItemKind
from assistant.models.digests import DigestDelivery
from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.models.reminders import Reminder, ReminderStatus
from assistant.models.users import User
from assistant.services import calendar as calendar_service
from assistant.services import digests, motivation, notifications
from assistant.services import jobs as jobs_service
from assistant.services import reminders as reminders_service
from assistant.services import workouts as workouts_service
from assistant.services.users import upsert_user
from assistant.worker import registry


async def _user(session: AsyncSession, user_id: int = 81) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="F")
    # 00:00 UTC is always in the past today, so scheduled digest jobs are
    # claimable in tests.
    user.settings.digest_time = time(0, 0)
    # English so the English content assertions below stay meaningful.
    user.settings.language = "en"
    await session.commit()
    return user


async def _seed(session: AsyncSession, user: User) -> None:
    today = datetime.now(tz=UTC).date()
    await calendar_service.create_item(
        session, user, title="Team standup",
        starts_at=datetime.combine(today, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=12),
    )
    await calendar_service.create_item(
        session, user, title="Overdue report",
        due_at=datetime.now(UTC) - timedelta(hours=2),
    )
    await calendar_service.create_item(
        session, user, title="Workout: Legs", kind=ItemKind.task,
        starts_at=datetime.now(UTC) + timedelta(days=1), source="workout",
    )
    await workouts_service.log_workout(session, user, name="Push day", duration_minutes=45)
    await session.commit()


def _fake_sender() -> tuple[AsyncMock, list[tuple[int, str]]]:
    calls: list[tuple[int, str]] = []

    async def send(chat_id: int, text: str) -> None:
        calls.append((chat_id, text))

    return AsyncMock(side_effect=send), calls


# ---------------------------------------------------------------------------
# Digest content
# ---------------------------------------------------------------------------


async def test_build_digest_includes_sections(session: AsyncSession) -> None:
    user = await _user(session)
    await _seed(session, user)

    content = await digests.build_digest(session, user)

    assert "Good morning, F!" in content
    assert "Team standup" in content
    assert "Overdue" in content
    assert "Overdue report" in content
    assert "Workout: Legs" in content
    assert "this week" in content  # workout stats line


async def test_build_digest_motivation_disabled(session: AsyncSession) -> None:
    user = await _user(session)
    user.settings.motivation_enabled = False
    await session.commit()

    content = await digests.build_digest(session, user)
    assert "streak" not in content
    assert "momentum" not in content


async def test_build_digest_empty_day(session: AsyncSession) -> None:
    user = await _user(session)

    content = await digests.build_digest(session, user)

    assert "nothing scheduled" in content
    assert "clear" in content  # motivation line for an empty schedule


# ---------------------------------------------------------------------------
# Scheduling idempotency
# ---------------------------------------------------------------------------


async def test_schedule_todays_digest_is_idempotent(session: AsyncSession) -> None:
    user = await _user(session)

    first, created = await digests.schedule_todays_digest(session, user)
    assert created is True
    second, created = await digests.schedule_todays_digest(session, user)
    assert created is False
    assert second.id == first.id
    await session.commit()

    rows = (await session.scalars(select(DigestDelivery))).all()
    assert [r.id for r in rows] == [first.id]
    jobs = (
        await session.scalars(select(BackgroundJob).where(BackgroundJob.type == digests.DIGEST_JOB_TYPE))
    ).all()
    assert [j.id for j in jobs] == [first.job_id]
    local_today = datetime.now(tz=ZoneInfo("UTC")).date()
    assert jobs[0].idempotency_key == f"digest:{user.id}:{local_today.isoformat()}"


async def test_conflict_does_not_rollback_caller_transaction(
    session: AsyncSession,
) -> None:
    """Regression for the caller-transaction rollback hazard: a lost
    uniqueness race in ``schedule_todays_digest`` must take the
    ON CONFLICT path (no IntegrityError, no ``session.rollback()``),
    because a rollback here would discard the delivery/job rows the
    multi-user worker pass already flushed for earlier users."""
    user_a = await _user(session, user_id=83)
    user_b = await _user(session, user_id=84)

    # user_b's digest already exists (committed by the "concurrent" winner).
    delivery_b, _ = await digests.schedule_todays_digest(session, user_b)
    await session.commit()

    # In one open transaction: create for user_a, then hit the conflict
    # branch for user_b.
    delivery_a, created_a = await digests.schedule_todays_digest(session, user_a)
    assert created_a is True
    again_b, created_b = await digests.schedule_todays_digest(session, user_b)
    assert created_b is False
    assert again_b.id == delivery_b.id

    # user_a's row must survive the commit (a rollback would have lost it).
    await session.commit()
    rows = (
        await session.scalars(
            select(DigestDelivery).where(DigestDelivery.user_id == user_a.id)
        )
    ).all()
    assert [r.id for r in rows] == [delivery_a.id]
    assert delivery_a.job_id is not None


async def test_ensure_digest_jobs_schedules_all_then_is_noop(
    session: AsyncSession,
) -> None:
    await _user(session, user_id=81)
    await _user(session, user_id=82)

    created = await digests.ensure_digest_jobs(session)
    await session.commit()
    assert created == 2

    created = await digests.ensure_digest_jobs(session)
    await session.commit()
    assert created == 0
    assert len((await session.scalars(select(DigestDelivery))).all()) == 2


async def test_ensure_digest_jobs_works_with_and_without_settings_row(
    session: AsyncSession,
) -> None:
    """Regression for the production ``MissingGreenlet`` traceback: the
    worker's periodic digest pass read ``user.settings`` (a lazy
    relationship) inside ``schedule_todays_digest``. ``ensure_digest_jobs``
    must eager-load settings and succeed for users WITH and WITHOUT a
    ``UserSettings`` row, repeated passes included (worker stays alive).
    """
    await _user(session, user_id=91)  # has a UserSettings row
    session.add(User(id=92, first_name="G"))  # raw user, NO settings row
    await session.commit()

    created = await digests.ensure_digest_jobs(session)
    await session.commit()
    assert created == 2

    rows = (await session.scalars(select(DigestDelivery))).all()
    assert {r.user_id for r in rows} == {91, 92}
    jobs = (
        await session.scalars(
            select(BackgroundJob).where(BackgroundJob.type == digests.DIGEST_JOB_TYPE)
        )
    ).all()
    assert {j.user_id for j in jobs} == {91, 92}

    # Subsequent worker passes stay alive and are idempotent no-ops.
    assert await digests.ensure_digest_jobs(session) == 0
    assert await digests.ensure_digest_jobs(session) == 0


async def test_lazy_settings_access_without_eager_load_raises_missing_greenlet(
    session: AsyncSession,
) -> None:
    """Documents the hazard the digest pass must avoid: a plain
    ``select(User)`` leaves ``settings`` unloaded, and a synchronous
    attribute read in async context raises ``MissingGreenlet`` — so any
    code path that reads ``user.settings`` synchronously must
    eager-load it (``selectinload``), never lazy-load it.
    """
    await _user(session, user_id=93)
    await session.commit()
    session.expire_all()
    plain = (
        await session.scalars(select(User).where(User.id == 93))
    ).one()
    with pytest.raises(MissingGreenlet):
        _ = plain.settings


async def test_digest_job_is_due_immediately_for_past_time(
    session: AsyncSession,
) -> None:
    user = await _user(session)  # digest_time 00:00 UTC -> in the past
    delivery, _ = await digests.schedule_todays_digest(session, user)
    await session.commit()
    job = await session.get(BackgroundJob, delivery.job_id)
    assert job.available_at <= datetime.now(UTC)


# ---------------------------------------------------------------------------
# Delivery handler
# ---------------------------------------------------------------------------


async def _run_digest_job(session: AsyncSession, sender: AsyncMock) -> BackgroundJob:
    job = await jobs_service.claim_job(session, worker_id="test-worker")
    await session.commit()
    assert job is not None
    handler = registry.handlers[digests.DIGEST_JOB_TYPE]
    await handler(session, job)
    await session.commit()
    await jobs_service.complete_job(session, job.id, owner_token=job.locked_by)
    await session.commit()
    return job


async def test_digest_handler_sends_once_and_is_idempotent(
    session: AsyncSession, monkeypatch,
) -> None:
    user = await _user(session)
    await _seed(session, user)
    sender, calls = _fake_sender()
    monkeypatch.setattr(notifications, "send_text", sender)
    _, _ = await digests.schedule_todays_digest(session, user)
    await session.commit()

    await _run_digest_job(session, sender)
    assert len(calls) == 1
    assert calls[0][0] == user.id
    assert "Good morning" in calls[0][1]

    delivery = (await session.scalars(select(DigestDelivery))).one()
    assert delivery.sent_at is not None
    assert delivery.content == calls[0][1]

    # Replay the same handler (worker restart / job retry): no second send.
    job = await session.get(BackgroundJob, delivery.job_id)
    job.status = JobStatus.running.value
    await session.commit()
    await registry.handlers[digests.DIGEST_JOB_TYPE](session, job)
    await session.commit()
    assert len(calls) == 1


async def test_digest_handler_missing_delivery_is_safe(
    session: AsyncSession, monkeypatch,
) -> None:
    sender, calls = _fake_sender()
    monkeypatch.setattr(notifications, "send_text", sender)
    job = await jobs_service.create_job(
        session, type=digests.DIGEST_JOB_TYPE, payload={"digest_id": 999999}
    )
    job.status = JobStatus.running.value
    await session.commit()

    await registry.handlers[digests.DIGEST_JOB_TYPE](session, job)
    await session.commit()
    assert calls == []


async def test_digest_send_runs_with_no_transaction_open(
    session: AsyncSession, monkeypatch
) -> None:
    """V5.2 §2: no application transaction may be open while the digest
    Telegram send is in flight. The fake sender records the handler
    session's transaction state at the moment of the call."""
    user = await _user(session)
    calls: list[tuple[int, bool]] = []

    async def send(chat_id: int, text: str) -> None:
        calls.append((chat_id, session.in_transaction()))

    monkeypatch.setattr(notifications, "send_text", AsyncMock(side_effect=send))
    _, _ = await digests.schedule_todays_digest(session, user)
    await session.commit()

    job = await jobs_service.claim_job(session, worker_id="test-worker")
    await session.commit()
    assert job is not None
    await registry.handlers[digests.DIGEST_JOB_TYPE](session, job)
    await session.commit()

    assert len(calls) == 1
    assert calls[0][0] == user.id
    assert calls[0][1] is False


async def test_digest_handler_with_expired_lease_does_not_send(
    session: AsyncSession, monkeypatch
) -> None:
    """V5.2 §2: a stale (expired) lease must suppress the digest send; the
    new owner delivers. The delivery is not stamped."""
    user = await _user(session)
    sender, calls = _fake_sender()
    monkeypatch.setattr(notifications, "send_text", sender)
    _, _ = await digests.schedule_todays_digest(session, user)
    await session.commit()

    job = (
        await session.scalars(
            select(BackgroundJob).where(
                BackgroundJob.type == digests.DIGEST_JOB_TYPE
            )
        )
    ).one()
    job.status = JobStatus.running.value
    job.locked_by = "test-worker"
    job.lease_until = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()

    await registry.handlers[digests.DIGEST_JOB_TYPE](session, job)
    await session.commit()

    assert calls == []
    delivery = (await session.scalars(select(DigestDelivery))).one()
    assert delivery.sent_at is None


# ---------------------------------------------------------------------------
# Motivation
# ---------------------------------------------------------------------------


async def test_motivation_line_selection(session: AsyncSession) -> None:
    user = await _user(session)

    def line(**overrides: object) -> str | None:
        base = {
            "has_overdue": False,
            "has_upcoming_workout": False,
            "current_streak": 0,
            "schedule_empty": False,
            **overrides,
        }
        return motivation.motivational_line(user, motivation.MotivationState(**base))

    assert "momentum" in line(has_overdue=True)
    assert "fuel" in line(has_upcoming_workout=True)
    assert "streak" in line(current_streak=3)
    assert "clear" in line(schedule_empty=True)
    assert line() is not None

    user.settings.motivation_enabled = False
    await session.commit()
    assert motivation.motivational_line(
        user,
        motivation.MotivationState(
            has_overdue=True, has_upcoming_workout=False,
            current_streak=0, schedule_empty=False,
        ),
    ) is None


# ---------------------------------------------------------------------------
# Reminder delivery
# ---------------------------------------------------------------------------


async def test_reminder_handler_delivers(session: AsyncSession, monkeypatch) -> None:
    user = await _user(session)
    sender, calls = _fake_sender()
    monkeypatch.setattr(notifications, "send_text", sender)
    reminder = await reminders_service.create_reminder(
        session, user,
        fire_at=datetime.now(UTC) - timedelta(minutes=1),
        message="Standup soon",
    )
    await session.commit()

    job = await session.get(BackgroundJob, reminder.job_id)
    job.status = JobStatus.running.value
    # V5 §3: handler re-validates DB ownership before sending; give it a live
    # lease so the simulated run is treated as owned.
    job.locked_by = "test-worker"
    job.lease_until = datetime.now(UTC) + timedelta(minutes=5)
    await session.commit()
    await registry.handlers[reminders_service.REMINDER_SEND_JOB_TYPE](session, job)
    await session.commit()

    # The stored user text is sent unchanged, wrapped in the localized
    # "Reminder:" prefix in the user's current language (English here).
    assert calls == [(user.id, "Reminder: Standup soon")]
    assert (await session.get(Reminder, reminder.id)).status == ReminderStatus.sent.value


async def test_reminder_send_failure_requeues(session: AsyncSession, monkeypatch) -> None:
    user = await _user(session)

    async def boom(chat_id: int, text: str) -> None:
        raise RuntimeError("telegram down")

    monkeypatch.setattr(notifications, "send_text", boom)
    reminder = await reminders_service.create_reminder(
        session, user,
        fire_at=datetime.now(UTC) - timedelta(minutes=1),
        message="Standup soon",
    )
    await session.commit()
    reminder_id = reminder.id  # capture before any expire below

    job = await session.get(BackgroundJob, reminder.job_id)
    job_id = job.id  # capture before rollback can expire the ORM object
    job.status = JobStatus.running.value
    job.locked_by = "test-worker"
    job.lease_until = datetime.now(UTC) + timedelta(minutes=5)
    await session.commit()
    with pytest.raises(RuntimeError, match="telegram down"):
        await registry.handlers[reminders_service.REMINDER_SEND_JOB_TYPE](session, job)
    await session.rollback()
    await jobs_service.fail_job(
        session, job_id, owner_token="test-worker", error="telegram down"
    )
    await session.commit()

    job = await session.get(BackgroundJob, job_id)
    assert job.status == JobStatus.pending.value
    assert job.attempts == 1
    assert (await session.get(Reminder, reminder_id)).status == ReminderStatus.pending.value
