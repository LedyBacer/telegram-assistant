"""Durable reminder creation/cancellation and delivery handler (SPEC §8).

Reminders are persisted as rows plus a durable background job, so a worker
restart cannot lose a pending send. Delivery is idempotent: the handler only
acts on reminders still in the ``pending`` state, and ``sent_at`` is written
exactly once. ``create_job`` is idempotent on ``idempotency_key`` so a
retried create cannot enqueue a second delivery job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.calendar_items import CalendarItem
from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.models.reminders import Reminder, ReminderStatus
from assistant.models.users import User
from assistant.services.jobs import cancel_job, create_job

REMINDER_SEND_JOB_TYPE = "reminder_send"


def _idempotency_key(reminder_id: int) -> str:
    return f"reminder:{reminder_id}"


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
    Returns the created reminders (empty when the item has no ``starts_at``).
    """
    if item.starts_at is None:
        return []
    created: list[Reminder] = []
    for offset in offsets_minutes:
        fire_at = item.starts_at - timedelta(minutes=offset)
        reminder = await create_reminder(
            session,
            user,
            fire_at=fire_at,
            message=f"Reminder: {item.title}",
            calendar_item=item,
            trigger_type="item_linked",
            offset_minutes=offset,
        )
        created.append(reminder)
    return created


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
    limit: int = 100,
) -> list[Reminder]:
    """List the user's reminders ordered by fire time."""
    stmt = (
        select(Reminder)
        .where(Reminder.user_id == user.id)
        .order_by(Reminder.fire_at)
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(Reminder.status == status.value)
    return list((await session.scalars(stmt)).all())


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
    """Deliver a reminder: mark it sent exactly once (flush only).

    The actual Telegram message delivery is a later-milestone concern
    (motivation/digest services); this handler makes the durable send state
    idempotent so job retries/restarts never double-send.
    """
    if job.status != JobStatus.running.value:
        return
    reminder_id = job.payload.get("reminder_id")
    if reminder_id is None:
        raise ValueError("reminder_send job payload is missing reminder_id")
    reminder = await session.get(Reminder, reminder_id)
    if reminder is None:
        # Item (and with it the reminder) was deleted after the job was
        # queued; nothing to deliver.
        return
    if reminder.status != ReminderStatus.pending.value:
        # Already sent (retry/restart) or cancelled: idempotent no-op.
        return
    reminder.status = ReminderStatus.sent.value
    reminder.sent_at = datetime.now(UTC)
    await session.flush()


def _register_handler() -> None:
    """Register the delivery handler with the worker's handler registry."""
    from assistant.worker.registry import register_job_handler

    register_job_handler(REMINDER_SEND_JOB_TYPE)(_handle_reminder_send)


_register_handler()
