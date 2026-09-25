"""Built-in data-management action kinds (V5.4 §3).

Natural-language deletion of the user's own data:
* ``delete_file`` — remove an uploaded file and its derived embeddings. The
  payload may name the file by id (preferred) or by ``original_filename``
  (resolved to the unique matching row; an ambiguous match is refused). The
  executor returns the file's ``storage_key`` so the confirming surface can
  discard the on-disk artifact *after* the DB transaction commits — mirroring
  the existing REST ``DELETE /files/{id}`` path, never removing the file
  inside the (rollback-capable) executor.
* ``delete_fact`` — remove a single confirmed fact by id.

Both re-validate ownership via the services (which raise ``ValueError`` on
cross-user access, surfaced as a 400) and raise :class:`ActionStaleError` with
bounded stable codes when the target no longer exists or has drifted since the
proposal. The executor flushes only; the caller commits.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions import register_action_kind
from assistant.actions.calendar import ActionStaleError
from assistant.i18n import t
from assistant.models.files import UserFile
from assistant.models.users import User
from assistant.services import facts as facts_service
from assistant.services import files as files_service


def _lang(user: User) -> str:
    return user.settings.language if user.settings is not None else "ru"


# ---------------------------------------------------------------------------
# delete_file
# ---------------------------------------------------------------------------


class DeleteFilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: int | None = None
    filename: str | None = Field(default=None, max_length=255)
    # Set by the proposal baseline; excludes the field from the model-facing
    # payload so the model never has to supply it.
    expected_updated_at: datetime | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _require_identifier(self) -> DeleteFilePayload:
        if self.file_id is None and not self.filename:
            raise ValueError("delete_file requires file_id or filename")
        return self


async def _resolve_file(
    session: AsyncSession, user: User, payload: DeleteFilePayload
) -> UserFile:
    """Resolve the target file, re-validating ownership.

    By id (preferred) or by exact ``original_filename``; an ambiguous
    filename (more than one matching row) is refused, never guessed.
    """
    if payload.file_id is not None:
        file = await files_service.get_file(session, user, payload.file_id)
        if file is None:
            raise ActionStaleError("file_not_found")
        return file
    rows = (
        await session.scalars(
            select(UserFile).where(
                UserFile.user_id == user.id,
                UserFile.original_filename == payload.filename,
            )
        )
    ).all()
    if len(rows) == 0:
        raise ActionStaleError("file_not_found")
    if len(rows) > 1:
        raise ActionStaleError("file_ambiguous")
    return rows[0]


async def _file_baseline(
    session: AsyncSession, user: User, payload: DeleteFilePayload
) -> dict:
    # Tolerate an unresolvable target (missing / ambiguous / not owned): the
    # proposal is stored without a baseline, and the executor re-resolves and
    # raises the stable stale code at confirm time.
    try:
        file = await _resolve_file(session, user, payload)
    except ActionStaleError:
        return {}
    await session.refresh(file)
    return {"expected_updated_at": file.updated_at.isoformat()}


async def exec_delete_file(
    session: AsyncSession, user: User, payload: DeleteFilePayload
) -> dict:
    file = await _resolve_file(session, user, payload)
    await session.refresh(file)
    if payload.expected_updated_at is not None and file.updated_at != payload.expected_updated_at:
        raise ActionStaleError("file_changed")
    storage_key = await files_service.delete_file(session, user, file.id)
    if storage_key is None:
        # Lost the row to a concurrent delete between resolve and delete.
        raise ActionStaleError("file_not_found")
    # The on-disk artifact is discarded by the confirming surface AFTER the
    # commit (see ``files_service.discard_storage``); we only return the key.
    return {"file_id": file.id, "deleted": True, "storage_key": storage_key}


async def _preview_delete_file(
    session: AsyncSession, user: User, payload: DeleteFilePayload
) -> str:
    lang = _lang(user)
    try:
        file = await _resolve_file(session, user, payload)
    except ActionStaleError:
        # Cannot resolve a real name (missing or ambiguous): fall back to the
        # identifier the user asked for, so the preview is still deterministic.
        if payload.file_id is not None:
            return t(lang, "action.preview.delete_file.id", id=payload.file_id)
        return t(lang, "action.preview.delete_file", filename=payload.filename or "")
    return t(lang, "action.preview.delete_file", filename=file.original_filename)


# ---------------------------------------------------------------------------
# delete_fact
# ---------------------------------------------------------------------------


class DeleteFactPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: int
    expected_updated_at: datetime | None = Field(default=None, exclude=True)


async def _fact_baseline(
    session: AsyncSession, user: User, payload: DeleteFactPayload
) -> dict:
    fact = await facts_service.get_fact(session, user, payload.fact_id)
    if fact is None:
        return {}
    await session.refresh(fact)
    return {"expected_updated_at": fact.updated_at.isoformat()}


async def exec_delete_fact(
    session: AsyncSession, user: User, payload: DeleteFactPayload
) -> dict:
    fact = await facts_service.get_fact(session, user, payload.fact_id)
    if fact is None:
        raise ActionStaleError("fact_not_found")
    await session.refresh(fact)
    if payload.expected_updated_at is not None and fact.updated_at != payload.expected_updated_at:
        raise ActionStaleError("fact_changed")
    deleted = await facts_service.delete_fact(session, user, payload.fact_id)
    if not deleted:
        raise ActionStaleError("fact_not_found")
    return {"fact_id": payload.fact_id, "deleted": True}


async def _preview_delete_fact(
    session: AsyncSession, user: User, payload: DeleteFactPayload
) -> str:
    lang = _lang(user)
    fact = await facts_service.get_fact(session, user, payload.fact_id)
    if fact is None:
        return t(lang, "action.preview.delete_fact.id", id=payload.fact_id)
    await session.refresh(fact)
    return t(lang, "action.preview.delete_fact", value=fact.value)


register_action_kind(
    "delete_file",
    payload_schema=DeleteFilePayload,
    executor=exec_delete_file,
    baseline=_file_baseline,
    preview=_preview_delete_file,
)
register_action_kind(
    "delete_fact",
    payload_schema=DeleteFactPayload,
    executor=exec_delete_fact,
    baseline=_fact_baseline,
    preview=_preview_delete_fact,
)
