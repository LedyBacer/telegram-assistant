"""Reminder endpoints (SPEC §8)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.api.auth import get_current_user
from assistant.api.routes.common import _aware, _bad_request, _not_found
from assistant.api.schemas import ReminderCreate, ReminderOut
from assistant.db import get_session
from assistant.models.reminders import ReminderStatus
from assistant.models.users import User
from assistant.services import reminders as reminders_service

router = APIRouter(tags=["miniapp"])


@router.get("/reminders", response_model=list[ReminderOut])
async def list_reminders(
    status: ReminderStatus | None = None,
    item_id: int | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ReminderOut]:
    reminders = await reminders_service.list_reminders(
        session, user, status=status, item_id=item_id
    )
    return [ReminderOut.model_validate(r) for r in reminders]


@router.post("/reminders", response_model=ReminderOut, status_code=201)
async def create_reminder(
    body: ReminderCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReminderOut:
    try:
        reminder = await reminders_service.create_reminder(
            session, user, fire_at=_aware(body.fire_at, user), message=body.message
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await session.commit()
    await session.refresh(reminder)
    return ReminderOut.model_validate(reminder)


@router.post("/reminders/{reminder_id}/cancel", response_model=ReminderOut)
async def cancel_reminder(
    reminder_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReminderOut:
    reminder = await reminders_service.cancel_reminder(session, user, reminder_id)
    if reminder is None:
        raise _not_found()
    await session.commit()
    await session.refresh(reminder)
    return ReminderOut.model_validate(reminder)
