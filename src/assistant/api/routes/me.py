"""Identity endpoint for the Mini App (SPEC §20)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from assistant.api.auth import get_current_user
from assistant.api.schemas import MeOut, SettingsOut, UserOut
from assistant.models.users import User

router = APIRouter(tags=["miniapp"])


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> MeOut:
    assert user.settings is not None  # upsert_user guarantees settings
    return MeOut(
        user=UserOut.model_validate(user),
        settings=SettingsOut.model_validate(user.settings),
    )
