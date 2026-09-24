"""Locale dictionary endpoints for the Mini App (single translation source)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from assistant.api.auth import get_current_user
from assistant.api.routes.common import _not_found
from assistant.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    is_supported,
    load_locale,
    t,
)
from assistant.models.users import User

router = APIRouter(tags=["miniapp"])


@router.get("/i18n/languages")
async def list_languages(user: User = Depends(get_current_user)) -> list[dict[str, str]]:
    """Supported languages with the label shown in the user's own language."""
    lang = user.settings.language if user.settings is not None else DEFAULT_LANGUAGE
    return [
        {"code": code, "label": t(lang, f"settings.language_{code}")}
        for code in sorted(SUPPORTED_LANGUAGES)
    ]


@router.get("/i18n/{locale}")
async def locale_dict(
    locale: str, user: User = Depends(get_current_user)
) -> dict[str, str]:
    """The flat translation dictionary for a supported locale."""
    if not is_supported(locale):
        raise _not_found()
    return load_locale(locale)
