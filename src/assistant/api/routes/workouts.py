"""Workout log + schedule endpoints (SPEC §10)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.api.auth import get_current_user
from assistant.api.routes.common import _aware, _bad_request
from assistant.api.schemas import (
    ItemOut,
    WorkoutCreate,
    WorkoutOut,
    WorkoutSchedule,
    WorkoutStatsOut,
)
from assistant.db import get_session
from assistant.models.users import User
from assistant.models.workout_logs import WorkoutStatus
from assistant.services import workouts as workouts_service

router = APIRouter(tags=["miniapp"])


@router.get("/workouts", response_model=list[WorkoutOut])
async def list_workouts(
    limit: int = Query(default=20, ge=1, le=100),
    status: WorkoutStatus | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[WorkoutOut]:
    logs = await workouts_service.list_workouts(
        session, user, limit=limit, status=status
    )
    return [WorkoutOut.model_validate(w) for w in logs]


@router.post("/workouts", response_model=WorkoutOut, status_code=201)
async def create_workout(
    body: WorkoutCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> WorkoutOut:
    try:
        log = await workouts_service.log_workout(
            session,
            user,
            name=body.name,
            started_at=_aware(body.started_at, user) if body.started_at else None,
            duration_minutes=body.duration_minutes,
            notes=body.notes,
            perceived_effort=body.perceived_effort,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await session.commit()
    await session.refresh(log)
    return WorkoutOut.model_validate(log)


@router.get("/workouts/stats", response_model=WorkoutStatsOut)
async def workout_stats(
    user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)
) -> WorkoutStatsOut:
    return WorkoutStatsOut(**await workouts_service.workout_stats(session, user))


@router.post("/workouts/schedule", response_model=ItemOut, status_code=201)
async def schedule_workout(
    body: WorkoutSchedule,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ItemOut:
    """Schedule a future workout: a calendar item plus a start-time reminder."""
    try:
        item = await workouts_service.schedule_workout(
            session,
            user,
            name=body.name,
            starts_at=_aware(body.starts_at, user),
            duration_minutes=body.duration_minutes,
            ends_at=_aware(body.ends_at, user) if body.ends_at else None,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await session.commit()
    await session.refresh(item)
    return ItemOut.model_validate(item)
