"""Shared helpers for the Mini App API domain routers (SPEC §20).

These are the cross-domain utilities that the per-domain routers share:
HTTP error constructors and timezone interpretation. Kept in one place so
every domain router behaves identically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from assistant.models.users import User


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="not found")


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _user_tz(user: User) -> ZoneInfo:
    name = user.settings.timezone if user.settings is not None else "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError):
        return ZoneInfo("UTC")


def _aware(value: datetime, user: User) -> datetime:
    """Naive datetimes are interpreted in the user's timezone (as services do)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=_user_tz(user))
    return value.astimezone(UTC)
