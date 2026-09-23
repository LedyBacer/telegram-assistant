"""Built-in workout action kinds (SPEC §10).

Conversational workout mutations: log a workout that just happened and
schedule a future one (calendar item + start-time reminder). Both executors
delegate to the shared workout service, so the same validation (name length,
duration, effort bounds, timezone normalization) applies as on every other
surface.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions import register_action_kind
from assistant.models.users import User
from assistant.services import workouts as workouts_service


class LogWorkoutPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    started_at: datetime | None = None
    duration_minutes: int | None = Field(default=None, gt=0)
    notes: str | None = Field(default=None, max_length=4000)
    perceived_effort: int | None = Field(default=None, ge=1, le=10)


class ScheduleWorkoutPayload(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    duration_minutes: int | None = Field(default=None, gt=0)


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
    )
    await session.flush()
    return {"item_id": item.id, "title": item.title}


register_action_kind(
    "log_workout", payload_schema=LogWorkoutPayload, executor=exec_log_workout
)
register_action_kind(
    "schedule_workout",
    payload_schema=ScheduleWorkoutPayload,
    executor=exec_schedule_workout,
)
