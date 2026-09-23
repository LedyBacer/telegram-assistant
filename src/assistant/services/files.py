"""Personal file upload, ingestion, and hybrid retrieval (SPEC §11-§13).

Uploads store only metadata (never the user-supplied filename is used as a
path). A durable ``files.ingest`` job drives the pipeline: download → extract
→ normalize → chunk → embed → store chunks → mark indexed. Each stage is
recorded so a stuck file is inspectable, and a failing stage is re-queued by
the worker with backoff (failures are visible on the ``user_files`` row and
retryable).

Retrieval is genuinely hybrid (SPEC §6, §13): the lexical full-text arm
(language-neutral ``simple`` text-search config) and the semantic vector arm
(pgvector cosine) run **independently** — a semantically-relevant chunk that
shares no exact words with the query is not filtered out by a lexical
prerequisite — and their ranked candidate sets are fused with Reciprocal Rank
Fusion. An embedding outage degrades to lexical-only results. Retrieval is
always scoped to the requesting user. Document text is untrusted user data.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import cast, delete, func, literal, select
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProvider, AIProviderError, get_ai_provider
from assistant.config import get_settings
from assistant.db import get_session_factory
from assistant.i18n import LocalizableError
from assistant.models.files import FileChunk, FileState, UserFile
from assistant.models.jobs import BackgroundJob, JobStatus
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
    """A retrieved chunk plus the source file it came from (for citations).

    ``score`` is the fused RRF score (higher is better). ``distance`` is the
    cosine distance of the chunk from the query in the vector arm (lower is
    better); it is ``None`` for chunks that were recalled lexically only
    (keyword overlap, no vector score). ``position`` is the chunk's index in
    the file; ``position_end`` is the index of the last chunk merged into this
    excerpt (``None`` for a single, unmerged chunk — treat it as ``position``).
    """

    file_id: int
    file_name: str
    position: int
    text: str
    score: float
    distance: float | None = None
    position_end: int | None = None


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


async def register_local_upload(
    session: AsyncSession,
    user: User,
    *,
    original_filename: str,
    mime_type: str,
    data: bytes,
) -> UserFile:
    """Register a Mini App upload (raw bytes) and enqueue ingestion.

    The bytes are written to a server-generated storage path. There is no
    Telegram file to download, so the ingestion pipeline reads the stored
    bytes directly (``telegram_file_id`` is ``None`` and
    ``extra.local_upload`` is set — see :func:`_run_pipeline`).
    """
    settings = get_settings()
    size_bytes = len(data)
    file = UserFile(
        user_id=user.id,
        storage_key=uuid.uuid4().hex,
        telegram_file_id=None,
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

    path = _storage_path(file.storage_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    file.extra = {**(file.extra or {}), "local_upload": True}
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


def discard_storage(storage_key: str) -> None:
    """Remove a stored file's disk artifact (orphan cleanup when the database
    registration of an upload fails; SPEC §21)."""
    _remove_disk_file(storage_key)


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


async def retry_file(session: AsyncSession, user: User, file_id: int) -> UserFile:
    """Re-queue ingestion for a failed file (flush only; the caller commits).

    Only ``failed`` files are retryable. ``rejected`` files stay rejected
    (the rejection is deterministic — unsupported type or oversize — so a
    re-run can never succeed). A file that is not a local upload is
    re-downloaded from Telegram; a local upload whose bytes are missing from
    disk cannot be retried and raises ``ValueError``.
    """
    file = await get_file(session, user, file_id)
    if file is None:
        raise ValueError("File not found.")
    if file.state == FileState.rejected.value:
        raise ValueError("File was rejected and cannot be re-ingested.")
    if file.state != FileState.failed.value:
        raise ValueError(f"File is {file.state}, not failed.")
    if (
        file.telegram_file_id is None
        and not _storage_path(file.storage_key).exists()
    ):
        raise ValueError("File data is missing and cannot be re-ingested.")
    if file.job_id is not None:
        await cancel_job(session, file.job_id)
    settings = get_settings()
    retry_count = int((file.extra or {}).get("retry_count", 0)) + 1
    job = await create_job(
        session,
        type=FILES_INGEST_JOB_TYPE,
        payload={"file_id": file.id},
        user_id=user.id,
        idempotency_key=f"file:{file.id}:retry:{retry_count}",
        max_attempts=settings.job_max_attempts,
    )
    file.job_id = job.id
    file.state = FileState.queued.value
    file.error = None
    file.indexed_at = None
    file.extra = {**(file.extra or {}), "retry_count": retry_count}
    await session.flush()
    return file


# ---------------------------------------------------------------------------
# Text extraction + chunking
# ---------------------------------------------------------------------------


#: Decompressed ``word/document.xml`` size bound (SPEC §21): a zip bomb must
#: not decompress into unbounded memory. 64 MB of raw XML is far beyond any
#: sane document.
_DOCX_XML_MAX_BYTES = 64 * 1024 * 1024


def extract_text(
    data: bytes,
    mime_type: str,
    *,
    max_chars: int,
    max_pdf_pages: int,
) -> str:
    """Extract plain text from a file's bytes. Raises FileUploadError for
    unsupported types, unreadable content, or content that exceeds the
    configured resource bounds (SPEC §21)."""
    if mime_type in ("text/plain", "text/markdown"):
        text = data.decode("utf-8", errors="replace")
    elif mime_type == "application/pdf":
        text = _extract_pdf(data, max_pages=max_pdf_pages)
    elif mime_type == DOCX_MIME:
        text = _extract_docx(data)
    else:
        raise FileUploadError(f"Unsupported file type: {mime_type}")
    if len(text) > max_chars:
        raise FileUploadError(
            f"extracted text is too long ({len(text)} > {max_chars} characters)"
        )
    return text


def _extract_pdf(data: bytes, *, max_pages: int) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as exc:
        raise FileUploadError(f"Could not read PDF: {exc}") from exc
    page_count = len(reader.pages)
    if page_count > max_pages:
        raise FileUploadError(
            f"PDF has too many pages ({page_count} > {max_pages})"
        )
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n\n".join(p for p in pages if p.strip())


def _extract_docx(data: bytes) -> str:
    """Extract text from a DOCX by streaming ``word/document.xml``.

    The entry is read in bounded chunks with a decompressed-size cap (zip-bomb
    guard); every ``<w:t>`` run — in paragraphs and tables alike — yields text.
    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise FileUploadError(f"Could not read DOCX: {exc}") from exc
    with archive:
        try:
            source = archive.open("word/document.xml")
        except KeyError as exc:
            raise FileUploadError("Not a valid DOCX (word/document.xml missing)") from exc
        parts: list[str] = []
        total = 0
        with source:
            while True:
                part = source.read(1 << 20)
                if not part:
                    break
                total += len(part)
                if total > _DOCX_XML_MAX_BYTES:
                    raise FileUploadError("DOCX content exceeds the decompression limit")
                parts.append(part.decode("utf-8", errors="replace"))
    runs = re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", "".join(parts))
    return "\n".join(html.unescape(run) for run in runs if run.strip())


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


async def _run_pipeline(
    session: AsyncSession, file: UserFile, job: BackgroundJob
) -> None:
    """Run download → extract → chunk → embed → store.

    §5.5: no PostgreSQL transaction is held across external I/O. Each state
    transition is committed before the (potentially slow) network call that
    follows, so the connection is released during the Telegram download and
    the embedding model call. Chunks are written in a single short final
    transaction, and that commit is skipped if the job was cancelled while
    ingesting — a cancelled run can therefore never commit stale chunks.
    """
    settings = get_settings()
    provider = get_ai_provider()

    destination = _storage_path(file.storage_key)
    destination.parent.mkdir(parents=True, exist_ok=True)

    # Download (external I/O) — commit the state first so no tx is held.
    await _set_file_state(session, file, FileState.downloading)
    await session.commit()
    if file.telegram_file_id is None:
        # Mini App upload: the bytes were already written to ``destination`` by
        # ``register_local_upload``; skip the Telegram download.
        if not (file.extra or {}).get("local_upload"):
            raise FileUploadError("File has no Telegram file id to download from.")
    else:
        await _download_telegram_file(file.telegram_file_id, destination)

    # Extract (CPU, local) — off the event loop so a large or hostilely
    # compressed document cannot starve the worker's heartbeat and other jobs
    # (SPEC §21).
    await _set_file_state(session, file, FileState.extracting)
    await session.commit()
    data = await asyncio.to_thread(destination.read_bytes)
    text = await asyncio.to_thread(
        extract_text,
        data,
        file.mime_type,
        max_chars=settings.max_extracted_text_chars,
        max_pdf_pages=settings.max_pdf_pages,
    )

    # Chunk (CPU, local).
    await _set_file_state(session, file, FileState.chunking)
    await session.commit()
    chunks = await asyncio.to_thread(
        chunk_text, text, settings.chunk_size, settings.chunk_overlap
    )
    # Total embedding batches and chunk rows are bounded per file (SPEC §21).
    if len(chunks) > settings.max_chunks_per_file:
        raise FileUploadError(
            f"document is too large to index ({len(chunks)} chunks > "
            f"{settings.max_chunks_per_file})"
        )

    # Embed (external I/O) — commit before so no tx spans the model call.
    vectors: list[list[float]] = []
    if chunks:
        await _set_file_state(session, file, FileState.embedding)
        await session.commit()
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

    # Cancellation check immediately before the only chunk-writing commit: a
    # job cancelled while downloading/embedding must not store stale chunks.
    # populate_existing re-reads from the DB (the job was loaded as "running"
    # at claim time; a concurrent API cancel commits in a different session).
    refreshed = await session.get(BackgroundJob, job.id, populate_existing=True)
    if refreshed is not None and refreshed.status == JobStatus.cancelled.value:
        await session.rollback()
        return

    # Final short transaction: replace any chunks from a previous run.
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
    file.extra = {
        **file.extra,
        "char_count": sum(len(c) for c in chunks),
        "chunk_count": len(chunks),
    }
    await session.commit()


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
        await _run_pipeline(session, file, job)
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
# Conditional hybrid retrieval (SPEC §6)
# ---------------------------------------------------------------------------

#: Reciprocal Rank Fusion constant (Cormack et al., 2009).
RRF_K = 60
#: Independent candidates fetched per list (lexical and vector) before fusion.
CANDIDATE_LIMIT = 10
#: Minimum fused RRF score; a candidate ranked at the tail of one list.
RRF_MIN_SCORE = 1.0 / (RRF_K + CANDIDATE_LIMIT)
#: Merged adjacent chunks are truncated so the context stays bounded.
MERGED_TEXT_LIMIT = 800

# The language-neutral text-search config (SPEC §6.1), cast to regconfig so
# PostgreSQL resolves to_tsvector/to_tsquery (a bare string literal is not an
# implicit cast target for the function overload).
_TS_CONFIG = cast(literal("simple"), REGCONFIG)


def _tsvector() -> Any:
    return func.to_tsvector(_TS_CONFIG, FileChunk.text)


def _lexical_tsquery(query: str) -> Any | None:
    """An OR-style, normalized ``tsquery`` for the lexical arm (P19, SPEC §13).

    The query is split into whitespace-delimited terms, each lowercased and
    stripped of every non-word character so arbitrary user input can never be
    read as a ``to_tsquery`` operator. Terms are joined with ``|`` (OR), so a
    chunk matches when it contains *any* of the query's words rather than all
    of them — restoring recall for multi-word queries that only partially
    overlap a chunk. Returns ``None`` when the query has no usable terms.
    """
    terms = [re.sub(r"[^\w]+", "", term.lower()) for term in query.split()]
    terms = [t for t in terms if t]
    if not terms:
        return None
    return func.to_tsquery(_TS_CONFIG, " | ".join(terms))


async def _lexical_candidates(
    session: AsyncSession, user: User, query: str
) -> list[tuple[FileChunk, str]]:
    """User chunks matching the query lexically, ranked by ts_rank desc."""
    tsquery = _lexical_tsquery(query)
    if tsquery is None:
        return []
    rank = func.ts_rank(_tsvector(), tsquery).label("lex_rank")
    rows = (
        (
            await session.execute(
                select(FileChunk, UserFile.original_filename, rank)
                .join(UserFile, FileChunk.file_id == UserFile.id)
                .where(
                    FileChunk.user_id == user.id,
                    _tsvector().op("@@")(tsquery),
                )
                .order_by(rank.desc(), FileChunk.position.asc())
                .limit(CANDIDATE_LIMIT)
            )
        )
        .all()
    )
    return [(chunk, name) for chunk, name, _ in rows]


async def _vector_candidates(
    session: AsyncSession,
    user: User,
    query: str,
    provider: AIProvider,
) -> list[tuple[FileChunk, str, float]]:
    """User chunks nearest the query embedding, with their cosine distance.

    Candidates are ranked by distance ascending and filtered to those within
    ``settings.retrieval_max_distance`` (the meaningful-relevance bound,
    SPEC §13, §17): a semantically off-topic chunk is never returned just
    because it happens to be the nearest.
    """
    query_vec = await provider.embed_query(query=query)
    max_distance = get_settings().retrieval_max_distance
    distance = FileChunk.embedding.cosine_distance(query_vec).label("distance")
    rows = (
        (
            await session.execute(
                select(FileChunk, UserFile.original_filename, distance)
                .join(UserFile, FileChunk.file_id == UserFile.id)
                .where(
                    FileChunk.user_id == user.id,
                    FileChunk.embedding.is_not(None),
                )
                .order_by(distance.asc(), FileChunk.position.asc())
                .limit(CANDIDATE_LIMIT)
            )
        )
        .all()
    )
    return [
        (chunk, name, float(dist))
        for chunk, name, dist in rows
        if dist is not None and float(dist) <= max_distance
    ]


def _fuse(
    lexical: list[tuple[FileChunk, str]],
    vector: list[tuple[FileChunk, str, float]],
) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion of the two ranked candidate lists.

    Each fused chunk records its best (minimum) vector-arm cosine distance so
    the meaningful-relevance bound is observable downstream; a chunk recalled
    lexically only keeps ``distance=None``.
    """
    fused: dict[int, RetrievedChunk] = {}

    def _add(chunk: FileChunk, name: str, rank: int, dist: float | None) -> None:
        entry = fused.get(chunk.id)
        if entry is None:
            entry = RetrievedChunk(
                file_id=chunk.file_id,
                file_name=name,
                position=chunk.position,
                text=chunk.text,
                score=0.0,
                distance=dist,
            )
            fused[chunk.id] = entry
        entry.score += 1.0 / (RRF_K + rank)
        if dist is not None and (entry.distance is None or dist < entry.distance):
            entry.distance = dist

    for rank, (chunk, name) in enumerate(lexical, start=1):
        _add(chunk, name, rank, None)
    for rank, (chunk, name, dist) in enumerate(vector, start=1):
        _add(chunk, name, rank, dist)

    kept = [c for c in fused.values() if c.score >= RRF_MIN_SCORE]
    kept.sort(key=lambda c: (-c.score, c.file_name, c.position))
    return kept


def _position_end(chunk: RetrievedChunk) -> int:
    """The last position merged into ``chunk`` (its own index if unmerged)."""
    return chunk.position_end if chunk.position_end is not None else chunk.position


def _merge_adjacent(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Merge same-file chunks that are close in position into one excerpt.

    Chunks from the same file whose positions are within
    ``settings.retrieval_merge_gap`` steps of each other collapse into a single
    bounded excerpt (text truncated to ``MERGED_TEXT_LIMIT``). The input is
    score-sorted, so same-file chunks are not guaranteed to be contiguous —
    they are grouped by file and merged by position proximity, then the merged
    spans are re-ordered by their best constituent score. ``position_end``
    records the span so citations can show a contiguous range.
    """
    gap = get_settings().retrieval_merge_gap
    by_file: dict[int, list[RetrievedChunk]] = {}
    for chunk in chunks:
        by_file.setdefault(chunk.file_id, []).append(chunk)

    merged: list[RetrievedChunk] = []
    for group in by_file.values():
        group.sort(key=lambda c: c.position)
        current: RetrievedChunk | None = None
        for chunk in group:
            if current is not None and chunk.position - _position_end(current) <= gap:
                current.text = f"{current.text} {chunk.text}"[:MERGED_TEXT_LIMIT]
                current.position_end = chunk.position
                current.score = max(current.score, chunk.score)
                if chunk.distance is not None and (
                    current.distance is None or chunk.distance < current.distance
                ):
                    current.distance = chunk.distance
            else:
                if current is not None:
                    merged.append(current)
                current = RetrievedChunk(
                    file_id=chunk.file_id,
                    file_name=chunk.file_name,
                    position=chunk.position,
                    text=chunk.text,
                    score=chunk.score,
                    distance=chunk.distance,
                )
        if current is not None:
            merged.append(current)

    merged.sort(key=lambda c: (-c.score, c.file_name, c.position))
    return merged


async def retrieve_chunks(
    session: AsyncSession,
    user: User,
    query: str,
    *,
    top_k: int = 5,
    provider: AIProvider | None = None,
) -> list[RetrievedChunk]:
    """Hybrid retrieval scoped to the user (SPEC §6, §13).

    The lexical full-text arm (PostgreSQL ``simple`` config) and the semantic
    vector arm (pgvector cosine) run **independently** and their ranked
    candidate sets are fused with Reciprocal Rank Fusion (SPEC §17): there is
    no lexical prerequisite, so a semantically-relevant chunk that shares no
    exact words with the query is still a candidate.

    1. Lexical and vector candidate sets are fetched independently (the vector
       arm needs the embedding provider, so the read transaction is released
       before that network call).
    2. An embedding outage (``AIProviderError``) degrades to lexical-only
       results instead of breaking the turn.
    3. ``[]`` only when both arms are empty; adjacent chunks from the same
       file are merged and the context stays bounded to ``top_k``.

    Scores are RRF scores (higher is better). Citations are derived
    deterministically from the returned metadata via :func:`format_citations`.
    """
    query = (query or "").strip()
    if not query or top_k <= 0:
        return []
    provider = provider or get_ai_provider()

    lexical = await _lexical_candidates(session, user, query)
    # Phase A/B/C: release the read transaction before the embedding network
    # call so no DB connection is held across provider I/O.
    await session.commit()
    vector: list[tuple[FileChunk, str, float]] = []
    try:
        vector = await _vector_candidates(session, user, query, provider)
    except AIProviderError:
        logger.warning("embedding provider unavailable; using lexical-only retrieval")

    if not lexical and not vector:
        return []
    fused = _fuse(lexical, vector)
    return _merge_adjacent(fused)[:top_k]


def format_citations(chunks: list[RetrievedChunk]) -> str:
    """Deterministic source line for bot responses (SPEC §6.3).

    Rendered by the application from retrieval metadata — it never depends
    on the model repeating source names. Sources are grouped by file; a file
    whose retrieved chunks span more than one position is annotated with the
    contiguous 1-based part range (``file.md (parts 2–3)``), so the citation
    reflects file *and* position proximity, not just the filename.
    """
    seen: dict[int, tuple[str, int, int]] = {}
    for c in chunks:
        start, end = c.position, _position_end(c)
        if c.file_id in seen:
            name, s, e = seen[c.file_id]
            seen[c.file_id] = (name, min(s, start), max(e, end))
        else:
            seen[c.file_id] = (c.file_name, start, end)
    if not seen:
        return ""
    parts: list[str] = []
    for name, start, end in seen.values():
        parts.append(f"{name} (parts {start + 1}–{end + 1})" if end > start else name)
    return "Sources: " + ", ".join(parts)
