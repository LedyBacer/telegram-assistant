"""Telegram Mini App initData verification (SPEC §19).

The frontend sends the raw ``Telegram.WebApp.initData`` string. This module
verifies the HMAC signature with the bot token, checks the ``auth_date``
freshness, and only then exposes the Telegram user identity.
``initDataUnsafe`` is never used as an authentication source.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.config import get_settings
from assistant.db import get_session
from assistant.models.users import User
from assistant.services.users import upsert_user

_HASH_RE = re.compile(r"[0-9a-f]{64}")
# Clock-skew tolerance: reject auth_date far in the future.
_FUTURE_TOLERANCE_SECONDS = 300


class InitDataError(ValueError):
    """initData is malformed, has an invalid signature, or is not fresh."""


def verify_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int,
) -> dict:
    """Validate initData and return the verified Telegram user object.

    Raises :class:`InitDataError` for any malformed, tampered, stale, or
    bot-issued payload.
    """
    params: dict[str, str] = {}
    signature: str | None = None
    for key, value in parse_qsl(init_data, keep_blank_values=True):
        if key == "hash":
            if signature is not None:
                raise InitDataError("duplicate hash parameter")
            signature = value
            continue
        if key in params:
            raise InitDataError(f"duplicate parameter {key!r}")
        params[key] = value
    if signature is None:
        raise InitDataError("hash is missing")
    if not _HASH_RE.fullmatch(signature):
        raise InitDataError("hash is not a valid sha256 hex string")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise InitDataError("signature mismatch")

    auth_date_raw = params.get("auth_date", "")
    if not auth_date_raw.isdigit():
        raise InitDataError("auth_date is missing")
    age = datetime.now(UTC).timestamp() - int(auth_date_raw)
    if age > max_age_seconds:
        raise InitDataError("initData is too old")
    if age < -_FUTURE_TOLERANCE_SECONDS:
        raise InitDataError("auth_date is in the future")

    user_raw = params.get("user")
    if not user_raw:
        raise InitDataError("user is missing")
    try:
        user = json.loads(user_raw)
    except ValueError as exc:
        raise InitDataError("user is not valid JSON") from exc
    if not isinstance(user, dict):
        raise InitDataError("user is not an object")
    user_id = user.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise InitDataError("user id is invalid")
    if user.get("is_bot"):
        raise InitDataError("bot users cannot open the Mini App")
    return user


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI dependency: verify initData, upsert, and return the user."""
    settings = get_settings()
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    try:
        tg_user = verify_init_data(
            init_data, settings.telegram_bot_token, settings.miniapp_auth_max_age_seconds
        )
    except InitDataError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    user, _ = await upsert_user(
        session,
        user_id=tg_user["id"],
        first_name=str(tg_user.get("first_name") or "User"),
        last_name=tg_user.get("last_name"),
        username=tg_user.get("username"),
    )
    await session.commit()
    return user
