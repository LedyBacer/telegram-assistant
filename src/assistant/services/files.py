"""Personal file upload, ingestion, and hybrid retrieval (SPEC §11-§13).

Uploads store only metadata (never the user-supplied filename is used as a
path). A durable ``files.ingest`` job drives the pipeline: download → extract
→ normalize → chunk → embed → store chunks → mark indexed. Each stage is
recorded so a stuck file is inspectable, and a failing stage is re-queued by
the worker with backoff (failures are visible on the ``user_files`` row and
retryable).

Retrieval is hybrid (semantic vector similarity + keyword relevance) and is
always scoped to the requesting user. Document text is untrusted user data.
"""

from __future__ import annotations

import io
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProvider, AIProviderError, get_ai_provider
from assistant.config import get_settings
from assistant.db import get_session_factory
from assistant.i18n import LocalizableError
from assistant.models.files import FileChunk, FileState, UserFile
from assistant.models.jobs import BackgroundJob
from assistant.models.users import User
from assistant.services.jobs import cancel_job, create_job

logger = logging.getLogger("assistant.files")

FILES_INGEST_JOB_TYPE = "files.ingest"

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

SUPPORTED_MIME_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/pdf",
        DOCX_MIME,
    }
)


class FileUploadError(ValueError):
    """The upload was rejected (unsupported type, oversize, etc.)."""


@dataclass(slots=True)
class RetrievedChunk:
    """A retrieved chunk plus the source file it came from (for citations)."""

    file_id: int
    file_name: str
    position: int
    text: str
    score: float


# ---------------------------------------------------------------------------
# Upload registration
# ---------------------------------------------------------------------------


async def register_upload(
    session: AsyncSession,
    user: User,
    *,
    original_filename: str,
    mime_type: str,
    size_bytes: int | None,
    telegram_file_id: str,
    telegram_file_unique_id: str | None = None,
) -> UserFile:
    """Register an uploaded file and enqueue its ingestion job.

    A server-generated UUID is used as the storage key; the supplied filename
    is stored for display only and never used to build a path (SPEC §11).
    Unsupported types and oversize uploads are persisted in the ``rejected``
    state so the user can see why, without an ingestion job.
    """
    settings = get_settings()
    file = UserFile(
        user_id=user.id,
        storage_key=uuid.uuid4().hex,
        telegram_file_id=telegram_file_id,
        telegram_file_unique_id=telegram_file_unique_id,
        original_filename=(original_filename or "unnamed")[:255],
        mime_type=(mime_type or "application/octet-stream")[:127],
        size_bytes=size_bytes,
        state=FileState.queued.value,
    )
    session.add(file)

    reason = _rejection_reason(file.mime_type, size_bytes, settings.max_upload_size_bytes)
    if reason is not None:
        file.state = FileState.rejected.value
        file.error = reason.key[:1000]
        file.extra = {**(file.extra or {}), "rejection": dict(reason.params)}
        await session.flush()
        return file

    await session.flush()
    job = await create_job(
        session,
        type=FILES_INGEST_JOB_TYPE,
        payload={"file_id": file.id},
        user_id=user.id,
        idempotency_key=f"file:{file.id}",
        max_attempts=settings.job_max_attempts,
    )
    file.job_id = job.id
    await session.flush()
    return file


def _rejection_reason(
    mime_type: str, size_bytes: int | None, max_bytes: int
) -> LocalizableError | None:
    """A localized rejection reason (locale key + params), or ``None``."""
    if mime_type not in SUPPORTED_MIME_TYPES:
        return LocalizableError("files.err_unsupported", mime=mime_type)
    if size_bytes is not None and size_bytes > max_bytes:
        return LocalizableError("files.err_too_large", size=size_bytes, max=max_bytes)
    return None


# ---------------------------------------------------------------------------
# Metadata access / lifecycle
# ---------------------------------------------------------------------------


async def get_file(
    session: AsyncSession, user: User, file_id: int
) -> UserFile | None:
    """Return a file belonging to the user, if any."""
    file = await session.get(UserFile, file_id)
    if file is None or file.user_id != user.id:
        return None
    return file


async def list_files(
    session: AsyncSession, user: User, *, limit: int = 20
) -> list[UserFile]:
    """List the user's files, newest first."""
    stmt = (
        select(UserFile)
        .where(UserFile.user_id == user.id)
        .order_by(UserFile.created_at.desc())
        .limit(limit)
    )
    return list((await session.scalars(stmt)).all())


async def delete_file(session: AsyncSession, user: User, file_id: int) -> bool:
    """Delete a file, its chunks, its disk artifact, and pending job.

    Returns ``True`` if a file was deleted, ``False`` if it was not found.
    """
    file = await get_file(session, user, file_id)
    if file is None:
        return False
    if file.job_id is not None:
        await cancel_job(session, file.job_id)
    await session.execute(delete(FileChunk).where(FileChunk.file_id == file_id))
    storage_key = file.storage_key
    file_id_to_delete = file.id
    await session.delete(file)
    await session.flush()
    _remove_disk_file(storage_key)
    return file_id_to_delete is not None


# ---------------------------------------------------------------------------
# Text extraction + chunking
# ---------------------------------------------------------------------------


def extract_text(data: bytes, mime_type: str) -> str:
    """Extract plain text from a file's bytes. Raises FileUploadError for
    unsupported types or unreadable content."""
    if mime_type in ("text/plain", "text/markdown"):
        return data.decode("utf-8", errors="replace")
    if mime_type == "application/pdf":
        return _extract_pdf(data)
    if mime_type == DOCX_MIME:
        return _extract_docx(data)
    raise FileUploadError(f"Unsupported file type: {mime_type}")


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise FileUploadError(f"Could not read PDF: {exc}") from exc
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n\n".join(p for p in pages if p.strip())


def _extract_docx(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Split normalized text into overlapping chunks at word boundaries.

    ``chunk_overlap`` characters of the previous chunk's tail are carried into
    the next chunk so context is not lost at boundaries.
    """
    normalized = " ".join(text.split())
    if not normalized:
        return []
    if len(normalized) <= chunk_size:
        return [normalized]

    chunks: list[str] = []
    start = 0
    length = len(normalized)
    while start < length:
        end = min(start + chunk_size, length)
        if end < length:
            # Prefer a space boundary in the second half of the window.
            space = normalized.rfind(" ", start + chunk_size // 2, end)
            if space > start:
                end = space
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(end - chunk_overlap, start + 1)
    return chunks


# ---------------------------------------------------------------------------
# Ingestion worker handler
# ---------------------------------------------------------------------------


async def _download_telegram_file(telegram_file_id: str, destination: Path) -> None:
    """Download a Telegram file to ``destination``. Isolated so tests can
    replace it without a real bot token."""
    from aiogram import Bot

    settings = get_settings()
    bot = Bot(token=settings.telegram_bot_token)
    try:
        tg_file = await bot.get_file(telegram_file_id)
        await bot.download_file(tg_file.file_path, destination=destination)
    finally:
        await bot.session.close()


def _storage_path(storage_key: str) -> Path:
    return Path(get_settings().file_storage_dir) / storage_key


def _remove_disk_file(storage_key: str) -> None:
    try:
        path = _storage_path(storage_key)
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("could not remove disk file for storage_key=%s", storage_key)


async def _set_file_state(
    session: AsyncSession, file: UserFile, state: FileState
) -> None:
    file.state = state.value
    await session.flush()


async def _run_pipeline(session: AsyncSession, file: UserFile) -> None:
    """Run download → extract → chunk → embed → store (flush only)."""
    settings = get_settings()
    provider = get_ai_provider()

    destination = _storage_path(file.storage_key)
    destination.parent.mkdir(parents=True, exist_ok=True)

    await _set_file_state(session, file, FileState.downloading)
    if file.telegram_file_id is None:
        raise FileUploadError("File has no Telegram file id to download from.")
    await _download_telegram_file(file.telegram_file_id, destination)

    await _set_file_state(session, file, FileState.extracting)
    data = destination.read_bytes()
    text = extract_text(data, file.mime_type)

    await _set_file_state(session, file, FileState.chunking)
    chunks = chunk_text(text, settings.chunk_size, settings.chunk_overlap)

    vectors: list[list[float]] = []
    if chunks:
        await _set_file_state(session, file, FileState.embedding)
        for i in range(0, len(chunks), settings.embedding_batch_size):
            # The provider applies the E5 document prefix; chunks are stored
            # in their original form.
            vectors.extend(
                await provider.embed_documents(
                    texts=chunks[i : i + settings.embedding_batch_size]
                )
            )
        if len(vectors) != len(chunks):
            raise AIProviderError(
                f"expected {len(chunks)} embeddings, got {len(vectors)}"
            )

    # Re-ingestion is idempotent: replace any chunks from a previous run.
    await session.execute(delete(FileChunk).where(FileChunk.file_id == file.id))
    for position, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
        session.add(
            FileChunk(
                user_id=file.user_id,
                file_id=file.id,
                position=position,
                text=chunk,
                char_count=len(chunk),
                embedding=vector,
            )
        )

    file.state = FileState.indexed.value
    file.indexed_at = datetime.now(UTC)
    file.error = None
    file.extra = {**file.extra, "char_count": sum(len(c) for c in chunks)}
    await session.flush()


async def _record_failure(file_id: int, error: str) -> None:
    """Persist a visible failure state in its own transaction so it survives
    the main pipeline transaction rolling back on error."""
    async with get_session_factory()() as failure_session:
        failed = await failure_session.get(UserFile, file_id)
        if failed is None:
            return
        failed.state = FileState.failed.value
        failed.error = (error or "ingestion failed")[:1000]
        await failure_session.commit()


async def handle_files_ingest(session: AsyncSession, job: BackgroundJob) -> None:
    """Worker handler: run the ingestion pipeline for one file (flush only)."""
    file_id = job.payload.get("file_id")
    if file_id is None:
        raise ValueError("files.ingest job payload is missing file_id")
    file = await session.get(UserFile, file_id)
    if file is None:
        # Deleted after the job was enqueued; nothing to do.
        return
    if file.state == FileState.indexed.value:
        # Idempotent: a retried/restarted job on an indexed file is a no-op.
        return
    try:
        await _run_pipeline(session, file)
    except Exception as exc:
        # Discard the failed pipeline work and release the row lock before
        # recording the visible failure state from a second connection.
        await session.rollback()
        await _record_failure(file_id, str(exc) or exc.__class__.__name__)
        raise


def _register_handler() -> None:
    from assistant.worker.registry import register_job_handler

    register_job_handler(FILES_INGEST_JOB_TYPE)(handle_files_ingest)


_register_handler()


# ---------------------------------------------------------------------------
# Hybrid retrieval
# ---------------------------------------------------------------------------


def _escape_ilike(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def retrieve_chunks(
    session: AsyncSession,
    user: User,
    query: str,
    *,
    top_k: int = 5,
    provider: AIProvider | None = None,
) -> list[RetrievedChunk]:
    """Hybrid retrieval scoped to the user (SPEC §13).

    Ranks the user's indexed chunks by semantic cosine similarity to the query
    embedding, with a small boost for chunks that also match the query as a
    substring. Returns the top ``top_k`` with their source filename for
    citation. Document text is treated as untrusted data, never instructions.
    """
    query = (query or "").strip()
    if not query or top_k <= 0:
        return []
    provider = provider or get_ai_provider()
    # The provider applies the E5 query prefix; the user's visible query is
    # left unmodified.
    query_vec = await provider.embed_query(query=query)

    distance = FileChunk.embedding.cosine_distance(query_vec).label("distance")
    semantic = (
        select(FileChunk, UserFile.original_filename, distance)
        .join(UserFile, FileChunk.file_id == UserFile.id)
        .where(
            FileChunk.user_id == user.id,
            FileChunk.embedding.is_not(None),
        )
        .order_by(distance.asc())
        .limit(top_k)
    )
    results: dict[int, RetrievedChunk] = {}
    for chunk, filename, dist in (await session.execute(semantic)).all():
        results[chunk.id] = RetrievedChunk(
            file_id=chunk.file_id,
            file_name=filename,
            position=chunk.position,
            text=chunk.text,
            score=1.0 - float(dist),
        )

    # Keyword boost for chunks that already rank semantically.
    term = query[:120]
    keyword_ids = (
        await session.scalars(
            select(FileChunk.id)
            .where(
                FileChunk.user_id == user.id,
                FileChunk.text.ilike(f"%{_escape_ilike(term)}%"),
            )
            .limit(top_k)
        )
    ).all()
    for cid in keyword_ids:
        if cid in results:
            results[cid].score += 0.25

    ordered = sorted(results.values(), key=lambda r: r.score, reverse=True)
    return ordered[:top_k]


def format_citations(chunks: list[RetrievedChunk]) -> str:
    """One line per distinct source file, for bot responses (SPEC §13)."""
    seen: dict[int, str] = {}
    for c in chunks:
        seen.setdefault(c.file_id, c.file_name)
    if not seen:
        return ""
    return "Sources: " + ", ".join(seen.values())
