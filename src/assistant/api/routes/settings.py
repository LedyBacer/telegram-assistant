"""User settings + proactive-delivery settings endpoints (SPEC §20)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.api.auth import get_current_user
from assistant.api.schemas import (
    ProactiveSettingsOut,
    ProactiveSettingsUpdate,
    SettingsOut,
    SettingsUpdate,
)
from assistant.db import get_session
from assistant.models.users import User
from assistant.services import proactivity as proactivity_service

router = APIRouter(tags=["miniapp"])


@router.get("/settings", response_model=SettingsOut)
async def read_settings(
    user: User = Depends(get_current_user),
) -> SettingsOut:
    assert user.settings is not None  # upsert_user guarantees settings
    return SettingsOut.model_validate(user.settings)


@router.patch("/settings", response_model=SettingsOut)
async def update_settings(
    body: SettingsUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> SettingsOut:
    assert user.settings is not None  # upsert_user guarantees settings
    if body.timezone is not None:
        user.settings.timezone = body.timezone
    if body.digest_time is not None:
        user.settings.digest_time = body.digest_time
    if body.motivation_enabled is not None:
        user.settings.motivation_enabled = body.motivation_enabled
    if body.language is not None:
        user.settings.language = body.language
    await session.commit()
    await session.refresh(user.settings)
    return SettingsOut.model_validate(user.settings)


@router.get("/proactive-settings", response_model=ProactiveSettingsOut)
async def read_proactive_settings(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProactiveSettingsOut:
    settings = await proactivity_service.get_proactive_settings(session, user.id)
    await session.commit()
    return ProactiveSettingsOut.model_validate(settings)


@router.patch("/proactive-settings", response_model=ProactiveSettingsOut)
async def update_proactive_settings(
    body: ProactiveSettingsUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ProactiveSettingsOut:
    settings = await proactivity_service.get_proactive_settings(session, user.id)
    for field in body.model_fields_set:
        setattr(settings, field, getattr(body, field))
    await session.commit()
    await session.refresh(settings)
    return ProactiveSettingsOut.model_validate(settings)
