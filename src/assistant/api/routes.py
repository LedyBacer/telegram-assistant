"""Authenticated Mini App API endpoints (SPEC §20).

Every route is user-scoped: the identity comes from verified Telegram
initData (see :mod:`assistant.api.auth`) and all service calls are
user-scoped, so one user can never read or modify another's data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.calendar import ActionStaleError
from assistant.ai import AIProviderError
from assistant.api.auth import get_current_user
from assistant.api.schemas import (
    ActionOut,
    FactCreate,
    FactOut,
    FactSupersede,
    FileOut,
    ItemCreate,
    ItemOut,
    ItemUpdate,
    MeOut,
    ProactiveSettingsOut,
    ProactiveSettingsUpdate,
    ReminderCreate,
    ReminderOut,
    SearchResultOut,
    SettingsOut,
    SettingsUpdate,
    UserOut,
    WorkoutCreate,
    WorkoutOut,
    WorkoutSchedule,
    WorkoutStatsOut,
)
from assistant.config import get_settings
from assistant.db import get_session
from assistant.i18n import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    is_supported,
    load_locale,
    t,
)
from assistant.models.facts import FactStatus
from assistant.models.pending_actions import ActionStatus
from assistant.models.reminders import ReminderStatus
from assistant.models.users import User
from assistant.models.workout_logs import WorkoutStatus
from assistant.services import actions as actions_service
from assistant.services import calendar as calendar_service
from assistant.services import facts as facts_service
from assistant.services import files as files_service
from assistant.services import proactivity as proactivity_service
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
    # Tri-state partial update (SPEC §4.3): an omitted key is left as-is
    # (UNSET), an explicit null clears the value, a value is set.
    present = body.model_fields_set
    datetime_fields = {"starts_at", "ends_at", "due_at"}

    def _tri(field: str):
        if field not in present:
            return calendar_service.UNSET
        value = getattr(body, field)
        if value is None:
            return None
        return _aware(value, user) if field in datetime_fields else value

    try:
        item = await calendar_service.update_item(
            session,
            user,
            item_id,
            title=_tri("title"),
            description=_tri("description"),
            starts_at=_tri("starts_at"),
            ends_at=_tri("ends_at"),
            due_at=_tri("due_at"),
            priority=_tri("priority"),
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
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await session.commit()
    await session.refresh(item)
    return ItemOut.model_validate(item)


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


@router.post("/files", response_model=FileOut, status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FileOut:
    """Upload a file from the Mini App (raw bytes).

    Mirrors the bot upload path but accepts multipart bytes directly, so the
    Files screen can register + index a document end to end. The ingestion job
    then reads the stored bytes (no Telegram download).
    """
    settings = get_settings()
    # Streamed read: abort as soon as the configured maximum is exceeded so an
    # oversized body is never buffered entirely in process memory (SPEC §21).
    parts: list[bytes] = []
    total = 0
    while True:
        part = await file.read(1024 * 1024)
        if not part:
            break
        total += len(part)
        if total > settings.max_upload_size_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        parts.append(part)
    data = b"".join(parts)
    created = None
    try:
        created = await files_service.register_local_upload(
            session,
            user,
            original_filename=file.filename or "unnamed",
            mime_type=file.content_type or "application/octet-stream",
            data=data,
        )
        await session.commit()
    except BaseException:
        # The database registration did not survive: do not leave an orphaned
        # upload on disk (SPEC §21).
        if created is not None:
            files_service.discard_storage(created.storage_key)
        raise
    await session.refresh(created)
    return FileOut.model_validate(created)


@router.post("/files/{file_id}/retry", response_model=FileOut)
async def retry_file_ingest(
    file_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FileOut:
    """Re-queue ingestion for a failed file. Rejected files cannot be retried."""
    try:
        file = await files_service.retry_file(session, user, file_id)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await session.commit()
    await session.refresh(file)
    return FileOut.model_validate(file)


@router.delete("/files/{file_id}", status_code=204)
async def delete_file(
    file_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    storage_key = await files_service.delete_file(session, user, file_id)
    if storage_key is None:
        raise _not_found()
    await session.commit()
    # Disk artifact is removed only AFTER the commit survives (P22): a failed
    # commit must not delete the only copy of a local upload.
    files_service.discard_storage(storage_key)
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


@router.post("/facts/{fact_id}/supersede", response_model=FactOut, status_code=201)
async def supersede_fact(
    fact_id: int,
    body: FactSupersede,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FactOut:
    """Propose a replacement: the referenced fact keeps its state, and the new
    value is created as ``proposed`` (linked via ``replaces_fact_id``); it
    only supersedes the old fact once the user confirms the new one."""
    try:
        fact = await facts_service.supersede_fact(
            session,
            user,
            fact_id,
            value=body.value,
            category=body.category,
            provenance="miniapp",
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    if fact is None:
        raise _not_found()
    await session.commit()
    await session.refresh(fact)
    return FactOut.model_validate(fact)


# ---------------------------------------------------------------------------
# Assistant Inbox — pending AI mutation proposals (SPEC §3)
# ---------------------------------------------------------------------------


@router.get("/actions", response_model=list[ActionOut])
async def list_actions(
    status: ActionStatus | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ActionOut]:
    actions = await actions_service.list_actions(
        session, user, status=status, limit=limit
    )
    return [ActionOut.model_validate(a) for a in actions]


@router.post("/actions/{action_id}/confirm", response_model=ActionOut)
async def confirm_action(
    action_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ActionOut:
    """Confirm and execute a proposed action in one transaction — the same
    flow the Telegram bot uses, so confirming here makes the action
    terminal for the bot and vice versa.

    Replaying confirmation on an already-executed action returns the stored
    state (execution itself is idempotent, SPEC §3). The confirm+execute runs
    under a row lock (:func:`confirm_and_execute_action`), so concurrent
    double-clicks cannot double-apply the mutation."""
    pre = await actions_service.get_action(session, user, action_id)
    if pre is None:
        raise _not_found()
    try:
        action, _result = await actions_service.confirm_and_execute_action(
            session, user, action_id
        )
    except ActionStaleError as exc:
        # The service marked the action ``expired`` in this transaction;
        # persist it before raising, because the session dependency rolls
        # back uncommitted work when the handler exits with an exception
        # (the action would otherwise stay proposed forever).
        await session.commit()
        raise HTTPException(
            status_code=409, detail="proposal no longer applies to current data"
        ) from exc
    except ValueError as exc:
        # Rejected/expired/no-longer-valid: persist any in-transaction expiry
        # the service recorded, then 400. (Not-found is handled above via the
        # pre-check; the service re-check only re-raises it in a delete race,
        # which we treat as "unavailable".)
        await session.commit()
        raise _bad_request(exc) from exc
    await session.commit()
    return ActionOut.model_validate(action)


@router.post("/actions/{action_id}/reject", response_model=ActionOut)
async def reject_action(
    action_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ActionOut:
    action = await actions_service.get_action(session, user, action_id)
    if action is None:
        raise _not_found()
    # Use effective status: an overdue proposed/confirmed action is rejected
    # (not executed/transitioned) and reported as expired without a write.
    if actions_service.effective_status(action) not in (
        ActionStatus.proposed.value,
        ActionStatus.confirmed.value,
    ):
        raise HTTPException(
            status_code=400, detail=f"action is {actions_service.effective_status(action)}"
        )
    action = await actions_service.reject_action(session, user, action_id)
    await session.commit()
    return ActionOut.model_validate(action)


# ---------------------------------------------------------------------------
# Settings (SPEC §20)
# ---------------------------------------------------------------------------


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
