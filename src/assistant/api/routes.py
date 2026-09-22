"""Authenticated Mini App API endpoints (SPEC §20).

Every route is user-scoped: the identity comes from verified Telegram
initData (see :mod:`assistant.api.auth`) and all service calls are
user-scoped, so one user can never read or modify another's data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProviderError
from assistant.api.auth import get_current_user
from assistant.api.schemas import (
    FactCreate,
    FactOut,
    FileOut,
    ItemCreate,
    ItemOut,
    ItemUpdate,
    MeOut,
    ReminderCreate,
    ReminderOut,
    SearchResultOut,
    SettingsOut,
    SettingsUpdate,
    UserOut,
    WorkoutCreate,
    WorkoutOut,
    WorkoutStatsOut,
)
from assistant.db import get_session
from assistant.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    is_supported,
    load_locale,
    t,
)
from assistant.models.facts import FactStatus
from assistant.models.reminders import ReminderStatus
from assistant.models.users import User
from assistant.models.workout_logs import WorkoutStatus
from assistant.services import calendar as calendar_service
from assistant.services import facts as facts_service
from assistant.services import files as files_service
from assistant.services import reminders as reminders_service
from assistant.services import workouts as workouts_service

router = APIRouter(prefix="/api/v1", tags=["miniapp"])


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


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> MeOut:
    assert user.settings is not None  # upsert_user guarantees settings
    return MeOut(
        user=UserOut.model_validate(user),
        settings=SettingsOut.model_validate(user.settings),
    )


# ---------------------------------------------------------------------------
# Calendar (SPEC §7)
# ---------------------------------------------------------------------------


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
    try:
        item = await calendar_service.update_item(
            session,
            user,
            item_id,
            title=body.title,
            description=body.description,
            starts_at=_aware(body.starts_at, user) if body.starts_at else None,
            ends_at=_aware(body.ends_at, user) if body.ends_at else None,
            due_at=_aware(body.due_at, user) if body.due_at else None,
            priority=body.priority,
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


# ---------------------------------------------------------------------------
# Workouts (SPEC §10)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Files (SPEC §11-13)
# ---------------------------------------------------------------------------


@router.get("/files", response_model=list[FileOut])
async def list_files(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[FileOut]:
    files = await files_service.list_files(session, user, limit=limit)
    return [FileOut.model_validate(f) for f in files]


@router.delete("/files/{file_id}", status_code=204)
async def delete_file(
    file_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    deleted = await files_service.delete_file(session, user, file_id)
    if not deleted:
        raise _not_found()
    await session.commit()
    return Response(status_code=204)


@router.get("/files/search", response_model=list[SearchResultOut])
async def search_files(
    q: str = Query(min_length=1, max_length=500),
    top_k: int = Query(default=5, ge=1, le=20),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[SearchResultOut]:
    try:
        chunks = await files_service.retrieve_chunks(session, user, q, top_k=top_k)
    except AIProviderError as exc:
        raise HTTPException(
            status_code=502, detail="embedding provider unavailable"
        ) from exc
    return [SearchResultOut.model_validate(c) for c in chunks]


# ---------------------------------------------------------------------------
# Reminders (SPEC §8)
# ---------------------------------------------------------------------------


@router.get("/reminders", response_model=list[ReminderOut])
async def list_reminders(
    status: ReminderStatus | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ReminderOut]:
    reminders = await reminders_service.list_reminders(session, user, status=status)
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


# ---------------------------------------------------------------------------
# Facts (SPEC §14)
# ---------------------------------------------------------------------------


@router.get("/facts", response_model=list[FactOut])
async def list_facts(
    status: FactStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[FactOut]:
    facts = await facts_service.list_facts(session, user, status=status, limit=limit)
    return [FactOut.model_validate(f) for f in facts]


@router.post("/facts", response_model=FactOut, status_code=201)
async def create_fact(
    body: FactCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FactOut:
    try:
        fact = await facts_service.propose_fact(
            session,
            user,
            value=body.value,
            category=body.category or "general",
            provenance="miniapp",
            confidence=body.confidence,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await session.commit()
    await session.refresh(fact)
    return FactOut.model_validate(fact)


@router.post("/facts/{fact_id}/confirm", response_model=FactOut)
async def confirm_fact(
    fact_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FactOut:
    fact = await facts_service.confirm_fact(session, user, fact_id)
    if fact is None:
        raise _not_found()
    await session.commit()
    await session.refresh(fact)
    return FactOut.model_validate(fact)


@router.post("/facts/{fact_id}/reject", response_model=FactOut)
async def reject_fact(
    fact_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FactOut:
    fact = await facts_service.reject_fact(session, user, fact_id)
    if fact is None:
        raise _not_found()
    await session.commit()
    await session.refresh(fact)
    return FactOut.model_validate(fact)


@router.delete("/facts/{fact_id}", status_code=204)
async def delete_fact(
    fact_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    deleted = await facts_service.delete_fact(session, user, fact_id)
    if not deleted:
        raise _not_found()
    await session.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Settings (SPEC §20)
# ---------------------------------------------------------------------------


@router.get("/settings", response_model=SettingsOut)
async def get_settings(
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


# ---------------------------------------------------------------------------
# i18n — locale dictionaries for the Mini App (single translation source)
# ---------------------------------------------------------------------------


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
