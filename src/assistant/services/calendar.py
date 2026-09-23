"""User-scoped calendar item operations (SPEC §7).

All timestamps are stored as UTC. Callers convert into the user's configured
timezone for display; the service accepts naive datetimes as being in the
user's timezone and normalizes them to UTC before persisting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.calendar_items import (
    CalendarItem,
    ItemKind,
    ItemPriority,
    ItemStatus,
)
from assistant.models.reminders import Reminder, ReminderStatus
from assistant.models.users import User

_ITEM_LIMIT = 200


def _to_utc(value: datetime | None, tz: ZoneInfo) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(UTC)


async def create_item(
    session: AsyncSession,
    user: User,
    *,
    title: str,
    kind: ItemKind = ItemKind.task,
    description: str | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    due_at: datetime | None = None,
    priority: ItemPriority = ItemPriority.normal,
    source: str = "bot",
    extra: dict | None = None,
) -> CalendarItem:
    """Create a calendar item with timestamps normalized to UTC."""
    if not title.strip():
        raise ValueError("Title is required.")
    if len(title) > 500:
        raise ValueError("Title is too long (max 500 characters).")
    if description and len(description) > 4000:
        raise ValueError("Description is too long (max 4000 characters).")

    tz = _user_tz(user)
    item = CalendarItem(
        user_id=user.id,
        kind=kind.value,
        title=title.strip(),
        description=description,
        starts_at=_to_utc(starts_at, tz),
        ends_at=_to_utc(ends_at, tz),
        due_at=_to_utc(due_at, tz),
        priority=priority.value,
        status=ItemStatus.scheduled.value,
        source=source,
        extra=extra or {},
    )
    session.add(item)
    await session.flush()
    return item


async def get_item(
    session: AsyncSession, user: User, item_id: int
) -> CalendarItem | None:
    """Return a calendar item belonging to the user, if any."""
    item = await session.get(CalendarItem, item_id)
    if item is None or item.user_id != user.id:
        return None
    return item


async def update_item(
    session: AsyncSession,
    user: User,
    item_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    due_at: datetime | None = None,
    priority: ItemPriority | None = None,
) -> CalendarItem | None:
    """Update mutable fields of a calendar item. ``None`` means "leave as-is"."""
    item = await get_item(session, user, item_id)
    if item is None:
        return None
    tz = _user_tz(user)
    if title is not None:
        title = title.strip()
        if not title:
            raise ValueError("Title cannot be empty.")
        if len(title) > 500:
            raise ValueError("Title is too long (max 500 characters).")
        item.title = title
    if description is not None:
        if len(description) > 4000:
            raise ValueError("Description is too long (max 4000 characters).")
        item.description = description
    if starts_at is not None:
        item.starts_at = _to_utc(starts_at, tz)
    if ends_at is not None:
        item.ends_at = _to_utc(ends_at, tz)
    if due_at is not None:
        item.due_at = _to_utc(due_at, tz)
    if priority is not None:
        item.priority = priority.value
    await session.flush()
    return item


async def complete_item(
    session: AsyncSession, user: User, item_id: int
) -> CalendarItem | None:
    """Mark an item as completed and record ``completed_at``."""
    item = await get_item(session, user, item_id)
    if item is None:
        return None
    if item.status != ItemStatus.completed.value:
        item.status = ItemStatus.completed.value
        item.completed_at = datetime.now(UTC)
        await session.flush()
    return item


async def cancel_item(
    session: AsyncSession, user: User, item_id: int
) -> CalendarItem | None:
    """Cancel an item and cancel its pending reminders (SPEC §8)."""
    item = await get_item(session, user, item_id)
    if item is None:
        return None
    if item.status != ItemStatus.cancelled.value:
        item.status = ItemStatus.cancelled.value
    reminders = (
        await session.scalars(
            select(Reminder).where(
                Reminder.calendar_item_id == item.id,
                Reminder.status == ReminderStatus.pending.value,
            )
        )
    ).all()
    for reminder in reminders:
        reminder.status = ReminderStatus.cancelled.value
        reminder.cancelled_at = datetime.now(UTC)
        if reminder.job_id is not None:
            from assistant.services.jobs import cancel_job

            await cancel_job(session, reminder.job_id)
    await session.flush()
    return item


async def delete_item(
    session: AsyncSession, user: User, item_id: int
) -> bool:
    """Delete a calendar item and its reminders. Returns True if deleted."""
    item = await get_item(session, user, item_id)
    if item is None:
        return False
    reminders = (
        await session.scalars(
            select(Reminder).where(Reminder.calendar_item_id == item.id)
        )
    ).all()
    for reminder in reminders:
        if reminder.job_id is not None:
            from assistant.services.jobs import cancel_job

            await cancel_job(session, reminder.job_id)
        await session.delete(reminder)
    await session.delete(item)
    await session.flush()
    return True


async def list_items(
    session: AsyncSession,
    user: User,
    *,
    start: datetime,
    end: datetime,
    status: ItemStatus | None = ItemStatus.scheduled,
    limit: int = _ITEM_LIMIT,
) -> list[CalendarItem]:
    """List items anchored to [start, end) via ``starts_at`` (or
    ``due_at`` when the item has no explicit start).

    The Mini App calendar groups items by their anchor date — the start when
    set, otherwise the due date. The query must mirror that, or a task created
    with only a due date would vanish from the month view. Pass ``status=None``
    to include every status (the month view keeps completed/cancelled items
    visible so they can be cleaned up).
    """
    start_utc = _to_utc(start, _user_tz(user))
    end_utc = _to_utc(end, _user_tz(user))
    anchor = func.coalesce(CalendarItem.starts_at, CalendarItem.due_at)
    where = [
        CalendarItem.user_id == user.id,
        anchor >= start_utc,
        anchor < end_utc,
    ]
    if status is not None:
        where.append(CalendarItem.status == status.value)
    items = (
        (
            await session.execute(
                select(CalendarItem)
                .where(*where)
                .order_by(anchor)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(items)


async def list_overdue(
    session: AsyncSession, user: User, *, limit: int = 20
) -> list[CalendarItem]:
    """List scheduled items whose ``due_at`` is in the past (SPEC §16)."""
    tz = _user_tz(user)
    items = (
        (
            await session.execute(
                select(CalendarItem)
                .where(
                    CalendarItem.user_id == user.id,
                    CalendarItem.status == ItemStatus.scheduled.value,
                    CalendarItem.due_at.is_not(None),
                    CalendarItem.due_at < datetime.now(tz=tz),
                )
                .order_by(CalendarItem.due_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(items)


async def list_today(
    session: AsyncSession, user: User
) -> list[CalendarItem]:
    """List the user's scheduled items for today in their local timezone."""
    tz = _user_tz(user)
    day = datetime.now(tz=tz).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=tz)
    return await list_items(
        session, user, start=start, end=start + timedelta(days=1)
    )


async def list_upcoming(
    session: AsyncSession, user: User, days: int = 7
) -> list[CalendarItem]:
    """List scheduled items starting from now through ``days`` ahead."""
    tz = _user_tz(user)
    now = datetime.now(tz=tz)
    return await list_items(
        session, user, start=now, end=now + timedelta(days=days)
    )


async def list_range(
    session: AsyncSession,
    user: User,
    *,
    start: datetime,
    end: datetime,
) -> list[CalendarItem]:
    """List all items (any status) within an explicit date range.

    The Mini App month view relies on this: completed/cancelled items must
    stay on their day so the UI can show a status badge and offer deletion.
    """
    if end <= start:
        raise ValueError("end must be after start.")
    return await list_items(session, user, start=start, end=end, status=None)


def _user_tz(user: User) -> ZoneInfo:
    name = user.settings.timezone if user.settings is not None else "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError):
        return ZoneInfo("UTC")
