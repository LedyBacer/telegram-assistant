"""Built-in calendar action kinds (SPEC §3).

Every executor re-fetches the entity from the payload (no trust in the
stored row beyond the schema), checks ownership and the expected entity
state, and raises :class:`ActionStaleError` when the action no longer
makes sense (item deleted, cancelled, ...). The service then expires the
action instead of executing a stale mutation.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, conint
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions import register_action_kind
from assistant.models.calendar_items import ItemKind, ItemPriority, ItemStatus
from assistant.models.users import User
from assistant.services import calendar as calendar_service
from assistant.services import reminders as reminders_service
from assistant.services.reminders import (
    MAX_OFFSET_MINUTES,
    MAX_REMINDERS_PER_ITEM,
    MIN_OFFSET_MINUTES,
)


# Raised by an executor when the target entity no longer exists, is no
# longer owned by the user, or is in a state that makes the mutation
# meaningless. The service transitions the action to ``expired``.
class ActionStaleError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _user_tz(user: User) -> ZoneInfo:
    name = user.settings.timezone if user.settings is not None else "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError):
        return ZoneInfo("UTC")


def _aware(value: datetime | None, user: User) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=_user_tz(user))
    return value.astimezone(ZoneInfo("UTC"))


# ---------------------------------------------------------------------------
# Payload schemas
# ---------------------------------------------------------------------------


class CreateItemPayload(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    kind: ItemKind = ItemKind.task
    description: str | None = Field(default=None, max_length=4000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    due_at: datetime | None = None
    priority: ItemPriority = ItemPriority.normal
    remind_offsets_minutes: list[
        conint(ge=MIN_OFFSET_MINUTES, le=MAX_OFFSET_MINUTES)
    ] = Field(default_factory=list, max_length=MAX_REMINDERS_PER_ITEM)


class UpdateItemPayload(BaseModel):
    """Partial update: only fields present in the dict are applied; an
    explicit null clears the value (SPEC §4.3).

    ``expected_updated_at`` is internal: it is captured at proposal time
    (the kind's baseline) and re-checked at execution to reject a mutation
    whose target drifted since the preview (optimistic guard, SPEC §3).
    ``exclude=True`` keeps the internal field out of the generated prompt
    docs — the model must never set it."""

    item_id: int
    title: str | None = None
    description: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    due_at: datetime | None = None
    priority: ItemPriority | None = None
    expected_updated_at: datetime | None = Field(default=None, exclude=True)


class CompleteItemPayload(BaseModel):
    item_id: int
    expected_updated_at: datetime | None = Field(default=None, exclude=True)


class CancelItemPayload(BaseModel):
    item_id: int
    expected_updated_at: datetime | None = Field(default=None, exclude=True)


class DeleteItemPayload(BaseModel):
    item_id: int
    expected_updated_at: datetime | None = Field(default=None, exclude=True)


class CreateReminderPayload(BaseModel):
    fire_at: datetime
    message: str = Field(min_length=1, max_length=1000)


class CancelReminderPayload(BaseModel):
    reminder_id: int


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------


async def _require_item(
    session: AsyncSession, user: User, item_id: int
) -> Any:
    item = await calendar_service.get_item(session, user, item_id)
    if item is None:
        raise ActionStaleError("calendar item no longer exists")
    # Load current state in the async context (avoids a lazy refresh on an
    # expired identity-mapped instance) so the stale + drift checks see the
    # real, committed row.
    await session.refresh(item)
    return item


async def _item_baseline(
    session: AsyncSession, user: User, payload: Any
) -> dict[str, object]:
    """Proposal-time capture of the target item's ``updated_at`` so the
    executor can detect drift (optimistic staleness guard, SPEC §3). A missing
    item is left empty — the executor will raise stale on it anyway."""
    item_id = getattr(payload, "item_id", None)
    if item_id is None:
        return {}
    item = await calendar_service.get_item(session, user, item_id)
    if item is None:
        return {}
    # Re-read in the async context so ``updated_at`` is loaded (not lazily
    # refreshed, which would raise MissingGreenlet on an expired instance).
    await session.refresh(item)
    return {"expected_updated_at": item.updated_at.isoformat()}


def _assert_not_drifted(item: Any, expected_updated_at: datetime | None) -> None:
    """Reject a mutation whose target changed since the proposal was made."""
    if expected_updated_at is not None and item.updated_at != expected_updated_at:
        raise ActionStaleError("calendar item changed since the proposal")


# ---------------------------------------------------------------------------
# Typed-data previews (deterministic user-facing summaries, SPEC §3)
# ---------------------------------------------------------------------------


def _fmt(value: datetime | None, user: User) -> str | None:
    """Format a datetime in the user's timezone, or None when unset."""
    if value is None:
        return None
    tz = _user_tz(user)
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


async def _item_title(session: AsyncSession, user: User, item_id: int) -> str:
    item = await calendar_service.get_item(session, user, item_id)
    if item is None:
        return f"#{item_id}"
    await session.refresh(item)
    return item.title


async def _preview_create_item(
    session: AsyncSession, user: User, payload: CreateItemPayload
) -> str:
    parts = [f"Create {payload.kind.value} '{payload.title}'"]
    starts = _fmt(payload.starts_at, user)
    if starts:
        parts.append(f"at {starts}")
    due = _fmt(payload.due_at, user)
    if due:
        parts.append(f"due {due}")
    if payload.remind_offsets_minutes:
        offsets = ", ".join(str(o) for o in payload.remind_offsets_minutes)
        parts.append(f"reminder(s) at {offsets} min")
    return " ".join(parts)


async def _preview_update_item(
    session: AsyncSession, user: User, payload: UpdateItemPayload
) -> str:
    title = await _item_title(session, user, payload.item_id)
    present = payload.model_fields_set - {"item_id", "expected_updated_at"}
    fields = ("title", "starts_at", "ends_at", "due_at", "priority", "description")
    changed = [name for name in fields if name in present]
    summary = ", ".join(changed) if changed else "(no fields)"
    return f"Update item '{title}': {summary}"


async def _preview_complete_item(
    session: AsyncSession, user: User, payload: CompleteItemPayload
) -> str:
    return f"Complete item '{await _item_title(session, user, payload.item_id)}'"


async def _preview_cancel_item(
    session: AsyncSession, user: User, payload: CancelItemPayload
) -> str:
    return f"Cancel item '{await _item_title(session, user, payload.item_id)}'"


async def _preview_delete_item(
    session: AsyncSession, user: User, payload: DeleteItemPayload
) -> str:
    return f"Delete item '{await _item_title(session, user, payload.item_id)}'"


async def _preview_create_reminder(
    session: AsyncSession, user: User, payload: CreateReminderPayload
) -> str:
    return f"Remind me '{payload.message}' at {_fmt(payload.fire_at, user)}"


async def _preview_cancel_reminder(
    session: AsyncSession, user: User, payload: CancelReminderPayload
) -> str:
    from assistant.models.reminders import Reminder

    reminder = await session.get(Reminder, payload.reminder_id)
    if reminder is None or reminder.user_id != user.id:
        return f"Cancel reminder #{payload.reminder_id}"
    return f"Cancel reminder '{reminder.message}'"


async def exec_create_item(
    session: AsyncSession, user: User, payload: CreateItemPayload
) -> dict:
    tz = _user_tz(user)
    item = await calendar_service.create_item(
        session,
        user,
        title=payload.title,
        kind=payload.kind,
        description=payload.description,
        starts_at=_aware(payload.starts_at, user),
        ends_at=_aware(payload.ends_at, user),
        due_at=_aware(payload.due_at, user),
        priority=payload.priority,
        source="assistant",
    )
    if payload.remind_offsets_minutes:
        await reminders_service.create_item_reminders(
            session, user, item, offsets_minutes=payload.remind_offsets_minutes
        )
    await session.flush()
    return {"item_id": item.id, "status": item.status, "tz": str(tz)}


async def exec_update_item(
    session: AsyncSession, user: User, payload: UpdateItemPayload
) -> dict:
    item = await _require_item(session, user, payload.item_id)
    _assert_not_drifted(item, payload.expected_updated_at)
    if item.status not in (
        ItemStatus.scheduled.value,
        ItemStatus.completed.value,
    ):
        raise ActionStaleError(f"item is {item.status}, not updatable")
    fields = payload.model_dump(exclude={"item_id", "expected_updated_at"})
    present = payload.model_fields_set
    update_kwargs = {}
    for name, value in fields.items():
        if name not in present:
            continue
        if name == "starts_at" or name == "ends_at" or name == "due_at":
            update_kwargs[name] = _aware(value, user)
        else:
            update_kwargs[name] = value
    updated = await calendar_service.update_item(
        session, user, item.id, **update_kwargs
    )
    assert updated is not None  # _require_item already checked
    await session.flush()
    return {"item_id": item.id, "title": updated.title, "status": updated.status}


async def exec_complete_item(
    session: AsyncSession, user: User, payload: CompleteItemPayload
) -> dict:
    item = await _require_item(session, user, payload.item_id)
    _assert_not_drifted(item, payload.expected_updated_at)
    if item.status != ItemStatus.scheduled.value:
        raise ActionStaleError(f"item is {item.status}, not schedulable")
    completed = await calendar_service.complete_item(session, user, item.id)
    assert completed is not None
    return {"item_id": item.id, "status": completed.status}


async def exec_cancel_item(
    session: AsyncSession, user: User, payload: CancelItemPayload
) -> dict:
    item = await _require_item(session, user, payload.item_id)
    _assert_not_drifted(item, payload.expected_updated_at)
    if item.status != ItemStatus.scheduled.value:
        raise ActionStaleError(f"item is {item.status}, not cancellable")
    cancelled = await calendar_service.cancel_item(session, user, item.id)
    assert cancelled is not None
    return {"item_id": item.id, "status": cancelled.status}


async def exec_delete_item(
    session: AsyncSession, user: User, payload: DeleteItemPayload
) -> dict:
    item = await _require_item(session, user, payload.item_id)
    _assert_not_drifted(item, payload.expected_updated_at)
    deleted = await calendar_service.delete_item(session, user, item.id)
    if not deleted:
        raise ActionStaleError("calendar item no longer exists")
    return {"item_id": item.id, "deleted": True}


async def exec_create_reminder(
    session: AsyncSession, user: User, payload: CreateReminderPayload
) -> dict:
    reminder = await reminders_service.create_reminder(
        session, user, fire_at=_aware(payload.fire_at, user), message=payload.message
    )
    return {"reminder_id": reminder.id, "fire_at": reminder.fire_at.isoformat()}


async def exec_cancel_reminder(
    session: AsyncSession, user: User, payload: CancelReminderPayload
) -> dict:
    from assistant.models.reminders import Reminder

    reminder = await session.get(Reminder, payload.reminder_id)
    if reminder is None or reminder.user_id != user.id:
        raise ActionStaleError("reminder no longer exists")
    cancelled = await reminders_service.cancel_reminder(
        session, user, payload.reminder_id
    )
    if cancelled is None:
        raise ActionStaleError("reminder no longer exists")
    return {"reminder_id": reminder.id, "status": cancelled.status}


register_action_kind(
    "create_item",
    payload_schema=CreateItemPayload,
    executor=exec_create_item,
    preview=_preview_create_item,
)
register_action_kind(
    "update_item",
    payload_schema=UpdateItemPayload,
    executor=exec_update_item,
    baseline=_item_baseline,
    preview=_preview_update_item,
)
register_action_kind(
    "complete_item",
    payload_schema=CompleteItemPayload,
    executor=exec_complete_item,
    baseline=_item_baseline,
    preview=_preview_complete_item,
)
register_action_kind(
    "cancel_item",
    payload_schema=CancelItemPayload,
    executor=exec_cancel_item,
    baseline=_item_baseline,
    preview=_preview_cancel_item,
)
register_action_kind(
    "delete_item",
    payload_schema=DeleteItemPayload,
    executor=exec_delete_item,
    baseline=_item_baseline,
    preview=_preview_delete_item,
)
register_action_kind(
    "create_reminder",
    payload_schema=CreateReminderPayload,
    executor=exec_create_reminder,
    preview=_preview_create_reminder,
)
register_action_kind(
    "cancel_reminder",
    payload_schema=CancelReminderPayload,
    executor=exec_cancel_reminder,
    preview=_preview_cancel_reminder,
)
