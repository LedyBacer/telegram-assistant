"""File upload, retry, delete, and search endpoints (SPEC §11-13)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProviderError
from assistant.api.auth import get_current_user
from assistant.api.routes.common import _bad_request, _not_found
from assistant.api.schemas import FileOut, SearchResultOut
from assistant.config import get_settings
from assistant.db import get_session
from assistant.models.users import User
from assistant.services import files as files_service

router = APIRouter(tags=["miniapp"])


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
