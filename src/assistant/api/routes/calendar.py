"""Calendar + task/event item endpoints (SPEC §7)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.api.auth import get_current_user
from assistant.api.routes.common import _aware, _bad_request, _not_found
from assistant.api.schemas import ItemCreate, ItemOut, ItemUpdate
from assistant.db import get_session
from assistant.models.users import User
from assistant.services import calendar as calendar_service
from assistant.services import reminders as reminders_service

router = APIRouter(tags=["miniapp"])


@router.get("/calendar/today", response_model=list[ItemOut])
async def calendar_today(
    user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> list[ItemOut]:
    items = await calendar_service.list_today(session, user)
    return [ItemOut.model_validate(i) for i in items]


@router.get("/calendar/upcoming", response_model=list[ItemOut])
async def calendar_upcoming(
    days: int = Query(default=7, ge=1, le=90),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ItemOut]:
    items = await calendar_service.list_upcoming(session, user, days=days)
    return [ItemOut.model_validate(i) for i in items]


@router.get("/items", response_model=list[ItemOut])
async def items_range(
    start: datetime,
    end: datetime,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ItemOut]:
    start = _aware(start, user)
    end = _aware(end, user)
    if end <= start:
        raise HTTPException(status_code=422, detail="end must be after start")
    items = await calendar_service.list_range(session, user, start=start, end=end)
    return [ItemOut.model_validate(i) for i in items]


@router.post("/items", response_model=ItemOut, status_code=201)
async def create_item(
    body: ItemCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemOut:
    try:
        item = await calendar_service.create_item(
            session,
            user,
            title=body.title,
            kind=body.kind,
            description=body.description,
            starts_at=_aware(body.starts_at, user) if body.starts_at else None,
            ends_at=_aware(body.ends_at, user) if body.ends_at else None,
            due_at=_aware(body.due_at, user) if body.due_at else None,
            priority=body.priority,
            source="miniapp",
        )
        if body.remind_offsets_minutes:
            await reminders_service.create_item_reminders(
                session, user, item, offsets_minutes=body.remind_offsets_minutes
            )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await session.commit()
    await session.refresh(item)
    return ItemOut.model_validate(item)


@router.get("/items/{item_id}", response_model=ItemOut)
async def get_item(
    item_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemOut:
    item = await calendar_service.get_item(session, user, item_id)
    if item is None:
        raise _not_found()
    return ItemOut.model_validate(item)


@router.patch("/items/{item_id}", response_model=ItemOut)
async def update_item(
    item_id: int,
    body: ItemUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemOut:
    # Tri-state partial update (SPEC §4.3): an omitted key is left as-is
    # (UNSET), an explicit null clears the value, a value is set.
    present = body.model_fields_set
    datetime_fields = {"starts_at", "ends_at", "due_at"}

    def _tri(field: str):
        if field not in present:
            return calendar_service.UNSET
        value = getattr(body, field)
        if value is None:
            return None
        return _aware(value, user) if field in datetime_fields else value

    try:
        item = await calendar_service.update_item(
            session,
            user,
            item_id,
            title=_tri("title"),
            description=_tri("description"),
            starts_at=_tri("starts_at"),
            ends_at=_tri("ends_at"),
            due_at=_tri("due_at"),
            priority=_tri("priority"),
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    if item is None:
        raise _not_found()
    await session.commit()
    await session.refresh(item)
    return ItemOut.model_validate(item)


@router.post("/items/{item_id}/complete", response_model=ItemOut)
async def complete_item(
    item_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemOut:
    item = await calendar_service.complete_item(session, user, item_id)
    if item is None:
        raise _not_found()
    await session.commit()
    await session.refresh(item)
    return ItemOut.model_validate(item)


@router.post("/items/{item_id}/cancel", response_model=ItemOut)
async def cancel_item(
    item_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemOut:
    item = await calendar_service.cancel_item(session, user, item_id)
    if item is None:
        raise _not_found()
    await session.commit()
    await session.refresh(item)
    return ItemOut.model_validate(item)


@router.delete("/items/{item_id}", status_code=204)
async def delete_item(
    item_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    deleted = await calendar_service.delete_item(session, user, item_id)
    if not deleted:
        raise _not_found()
    await session.commit()
    return Response(status_code=204)
