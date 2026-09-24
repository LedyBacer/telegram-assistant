"""Built-in workout action kinds (SPEC §10).

Conversational workout mutations: log a workout that just happened and
schedule a future one (calendar item + start-time reminder). Both executors
delegate to the shared workout service, so the same validation (name length,
duration, effort bounds, timezone normalization) applies as on every other
surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions import register_action_kind
from assistant.i18n import t
from assistant.models.users import User
from assistant.services import workouts as workouts_service


def _user_tz(user: User) -> ZoneInfo:
    name = user.settings.timezone if user.settings is not None else "UTC"
    try:
        return ZoneInfo(name)
    except (ValueError, KeyError):
        return ZoneInfo("UTC")


def _fmt(value: datetime | None, user: User) -> str | None:
    """Format a datetime in the user's timezone, or None when unset."""
    if value is None:
        return None
    tz = _user_tz(user)
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")


class LogWorkoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    started_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=4000)
    perceived_effort: int | None = Field(default=None, ge=1, le=10)


class ScheduleWorkoutPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    duration_minutes: int | None = Field(default=None, gt=0)
    ends_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_interval(self) -> ScheduleWorkoutPayload:
        # V5 §5.1: reject an inverted interval, and reject the two mutually
        # exclusive representations of the end (explicit end vs start+duration)
        # when they disagree.
        if (
            self.ends_at is not None
            and self.starts_at is not None
            and self.ends_at < self.starts_at
        ):
            raise ValueError("ends_at must be after starts_at")
        if (
            self.duration_minutes is not None
            and self.ends_at is not None
            and self.starts_at is not None
            and self.ends_at != self.starts_at + timedelta(minutes=self.duration_minutes)
        ):
            raise ValueError(
                "duration_minutes and ends_at must be consistent with starts_at"
            )
        return self


async def exec_log_workout(
    session: AsyncSession, user: User, payload: LogWorkoutPayload
) -> dict:
    log = await workouts_service.log_workout(
        session,
        user,
        name=payload.name,
        started_at=payload.started_at,
        duration_minutes=payload.duration_minutes,
        notes=payload.notes,
        perceived_effort=payload.perceived_effort,
    )
    await session.flush()
    return {"workout_id": log.id, "name": log.name}


async def exec_schedule_workout(
    session: AsyncSession, user: User, payload: ScheduleWorkoutPayload
) -> dict:
    item = await workouts_service.schedule_workout(
        session,
        user,
        name=payload.name,
        starts_at=payload.starts_at,
        duration_minutes=payload.duration_minutes,
        ends_at=payload.ends_at,
    )
    await session.flush()
    return {"item_id": item.id, "title": item.title}


def _lang(user: User) -> str:
    return user.settings.language if user.settings is not None else "ru"


async def _preview_log_workout(
    session: AsyncSession, user: User, payload: LogWorkoutPayload
) -> str:
    lang = _lang(user)
    parts = [t(lang, "action.preview.log_workout", name=payload.name)]
    if payload.duration_minutes:
        parts.append(t(lang, "action.preview.log_workout.duration", minutes=payload.duration_minutes))
    if payload.perceived_effort is not None:
        parts.append(t(lang, "action.preview.log_workout.effort", effort=payload.perceived_effort))
    return " ".join(parts)


async def _preview_schedule_workout(
    session: AsyncSession, user: User, payload: ScheduleWorkoutPayload
) -> str:
    lang = _lang(user)
    parts = [
        t(
            lang,
            "action.preview.schedule_workout",
            name=payload.name,
            when=_fmt(payload.starts_at, user),
        )
    ]
    if payload.ends_at:
        parts.append(t(lang, "action.preview.schedule_workout.until", when=_fmt(payload.ends_at, user)))
    elif payload.duration_minutes:
        parts.append(t(lang, "action.preview.schedule_workout.duration", minutes=payload.duration_minutes))
    return " ".join(parts)


register_action_kind(
    "log_workout",
    payload_schema=LogWorkoutPayload,
    executor=exec_log_workout,
    preview=_preview_log_workout,
)
register_action_kind(
    "schedule_workout",
    payload_schema=ScheduleWorkoutPayload,
    executor=exec_schedule_workout,
    preview=_preview_schedule_workout,
)
