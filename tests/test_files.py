"""File upload, ingestion, and hybrid retrieval tests (SPEC §11-§13).

Real PostgreSQL for the metadata lifecycle, chunk ownership isolation, and
retrieval filtering. The Telegram download and the AI embedding model are
faked, so no credentials or network access are required.
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProviderError
from assistant.bot.handlers import on_document
from assistant.config import get_settings
from assistant.models.files import EMBEDDING_DIMENSIONS, FileChunk, FileState, UserFile
from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.models.users import User
from assistant.services import files
from assistant.services import jobs as jobs_service
from assistant.services.users import upsert_user


async def _user(session: AsyncSession, user_id: int = 51) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="F")
    await session.commit()
    return user


class _FakeEmbedder:
    """Deterministic embedder: marker 0 if the text mentions 'quantum',
    marker 1 for 'coffee', marker 2 otherwise.

    Records the raw texts received from the service: they must arrive
    without E5 prefixes (the real provider applies them, not the caller).
    """

    def __init__(self) -> None:
        self.document_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        return [_marker_vector(self._marker(t)) for t in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        self.query_calls.append(query)
        return _marker_vector(self._marker(query))

    @staticmethod
    def _marker(text: str) -> int:
        if "quantum" in text.lower():
            return 0
        if "coffee" in text.lower():
            return 1
        return 2


def _marker_vector(marker: int) -> list[float]:
    vec = [0.0] * EMBEDDING_DIMENSIONS
    vec[marker] = 1.0
    return vec


class _MultiLingualEmbedder:
    """Embedder that treats EN/ES/RU 'coffee' terms as one shared semantic
    concept (one-hot marker 1). Cross-language retrieval is therefore pure
    vector closeness with no lexical overlap to fall back on."""

    _COFFEE = {"coffee", "café", "кофе", "kaffee"}

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        return [_marker_vector(self._marker(t)) for t in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        return _marker_vector(self._marker(query))

    @classmethod
    def _marker(cls, text: str) -> int:
        return 1 if {w for w in text.lower().split() if w in cls._COFFEE} else 2


async def _indexed_file(
    session: AsyncSession,
    user: User,
    *,
    filename: str,
    texts: list[str],
    marker_fn: Any = _FakeEmbedder._marker,
) -> UserFile:
    """Insert a file with pre-indexed chunks (bypasses the pipeline)."""
    file = UserFile(
        user_id=user.id,
        storage_key=uuid.uuid4().hex,
        original_filename=filename,
        mime_type="text/plain",
        size_bytes=sum(len(t) for t in texts),
        state=FileState.indexed.value,
    )
    session.add(file)
    await session.flush()
    for position, text in enumerate(texts):
        session.add(
            FileChunk(
                user_id=user.id,
                file_id=file.id,
                position=position,
                text=text,
                char_count=len(text),
                embedding=_marker_vector(marker_fn(text)),
            )
        )
    await session.commit()
    return file


async def _claim_file_job(session: AsyncSession, worker_id: str) -> BackgroundJob:
    job = await jobs_service.claim_job(session, worker_id=worker_id)
    await session.commit()
    assert job is not None
    return job


def _fake_download(content: bytes) -> Any:
    async def _download(telegram_file_id: str, destination: Path) -> None:
        destination.write_bytes(content)  # noqa: ASYNC240  (test double)

    return _download


# ---------------------------------------------------------------------------
# Upload registration (SPEC §11)
# ---------------------------------------------------------------------------


async def test_register_upload_enqueues_ingest_job(session) -> None:
    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="notes.txt",
        mime_type="text/plain",
        size_bytes=42,
        telegram_file_id="tg-file-1",
        telegram_file_unique_id="tg-uniq-1",
    )
    await session.commit()

    assert file.state == FileState.queued.value
    assert len(file.storage_key) == 32  # server-generated UUID hex
    assert file.storage_key == file.storage_key.lower()
    assert file.job_id is not None

    job = await session.get(BackgroundJob, file.job_id)
    assert job is not None
    assert job.type == files.FILES_INGEST_JOB_TYPE
    assert job.payload == {"file_id": file.id}
    assert job.idempotency_key == f"file:{file.id}"
    assert job.user_id == user.id


async def test_register_upload_rejects_unsupported_mime(session) -> None:
    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="photo.png",
        mime_type="image/png",
        size_bytes=10,
        telegram_file_id="tg-file-2",
    )
    await session.commit()

    assert file.state == FileState.rejected.value
    assert file.error == "files.err_unsupported"
    assert (file.extra or {})["rejection"] == {"mime": "image/png"}
    assert file.job_id is None
    jobs = (await session.scalars(select(BackgroundJob))).all()
    assert jobs == []


async def test_register_upload_rejects_oversize(session) -> None:
    user = await _user(session)
    max_bytes = get_settings().max_upload_size_bytes
    file = await files.register_upload(
        session,
        user,
        original_filename="big.pdf",
        mime_type="application/pdf",
        size_bytes=max_bytes + 1,
        telegram_file_id="tg-file-3",
    )
    await session.commit()

    assert file.state == FileState.rejected.value
    assert file.error == "files.err_too_large"
    rejection = (file.extra or {})["rejection"]
    assert rejection["size"] == max_bytes + 1
    assert rejection["max"] == max_bytes
    assert file.job_id is None


async def test_register_upload_rejects_when_embedding_unconfigured(
    session, monkeypatch
) -> None:
    """V3 P42: a chat-only deployment rejects a document upload at registration
    so it fails fast and visibly, with no ingestion job enqueued."""
    monkeypatch.setattr(get_settings(), "embedding_api_key", None)
    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="notes.txt",
        mime_type="text/plain",
        size_bytes=42,
        telegram_file_id="tg-file-p42",
    )
    await session.commit()

    assert file.state == FileState.rejected.value
    assert file.error == "files.err_embedding_unconfigured"
    assert file.job_id is None
    jobs = (await session.scalars(select(BackgroundJob))).all()
    assert jobs == []


async def test_register_local_upload_rejects_when_embedding_unconfigured(
    session, monkeypatch, tmp_path
) -> None:
    """V3 P42: a Mini App upload is rejected identically — no job, and the
    bytes are never written to disk for a rejected file."""
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(get_settings(), "embedding_api_key", None)
    user = await _user(session)
    file = await files.register_local_upload(
        session,
        user,
        original_filename="local.txt",
        mime_type="text/plain",
        data=b"hi",
    )
    await session.commit()

    assert file.state == FileState.rejected.value
    assert file.error == "files.err_embedding_unconfigured"
    assert file.job_id is None
    assert not (tmp_path / "files" / file.storage_key).exists()
    jobs = (await session.scalars(select(BackgroundJob))).all()
    assert jobs == []


async def test_register_local_upload_cleans_up_when_create_job_fails(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """V4 §30: the service owns the filesystem write — if ``create_job``
    (or the final flush) fails after the bytes are written, it must remove its
    own artifact, and the caller's transaction rollback leaves no DB row."""
    storage = tmp_path / "files"
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(storage))
    monkeypatch.setattr(get_settings(), "embedding_api_key", "test-key")
    user = await _user(session)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("injected create_job failure")

    monkeypatch.setattr(files, "create_job", _boom)

    with pytest.raises(RuntimeError, match="injected create_job failure"):
        await files.register_local_upload(
            session,
            user,
            original_filename="local.txt",
            mime_type="text/plain",
            data=b"hello orphan",
        )

    # The service removed its own disk artifact (no orphan).
    assert list(storage.iterdir()) == []
    # The flushed (uncommitted) row does not survive the rollback.
    await session.rollback()
    rows = list((await session.scalars(select(UserFile))).all())
    assert rows == []


# ---------------------------------------------------------------------------
# Extraction + chunking unit tests
# ---------------------------------------------------------------------------


def test_chunk_text_short_and_empty() -> None:
    assert files.chunk_text("", 100, 10) == []
    assert files.chunk_text("  hello   world  ", 100, 10) == ["hello world"]


def test_chunk_text_overlap_and_coverage() -> None:
    text = " ".join(f"w{i}" for i in range(50))
    chunks = files.chunk_text(text, chunk_size=100, chunk_overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    # The last word of each chunk reappears in the next (overlap carried).
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert prev.split()[-1] in nxt.split()
    joined = " ".join(chunks)
    assert all(f"w{i}" in joined for i in range(50))


def _extract(data: bytes, mime_type: str, **overrides: Any) -> str:
    kwargs: dict[str, Any] = {"max_chars": 100_000, "max_pdf_pages": 500}
    kwargs.update(overrides)
    return files.extract_text(data, mime_type, **kwargs)


def test_extract_text_plain_markdown_and_docx() -> None:
    assert _extract("héllo\n".encode(), "text/plain") == "héllo\n"
    assert _extract(b"# md", "text/markdown") == "# md"

    import docx as docx_lib

    buffer = io.BytesIO()
    document = docx_lib.Document()
    document.add_paragraph("Hello docx world")
    document.save(buffer)
    assert _extract(buffer.getvalue(), files.DOCX_MIME) == "Hello docx world"


def test_extract_text_unsupported_raises() -> None:
    with pytest.raises(files.FileUploadError, match="Unsupported"):
        _extract(b"x", "image/png")


def test_extract_text_enforces_character_bound() -> None:
    with pytest.raises(files.FileUploadError, match="too long"):
        _extract(b"a" * 5_000, "text/plain", max_chars=1_000)


def test_extract_docx_decompression_bomb_is_bounded(monkeypatch) -> None:
    import zipfile as zipfile_lib

    monkeypatch.setattr(files, "_DOCX_XML_MAX_BYTES", 1024)
    xml = "<w:document><w:body>" + "<w:t>ab" * 4096 + "</w:t></w:body></w:document>"
    buffer = io.BytesIO()
    with zipfile_lib.ZipFile(buffer, "w", zipfile_lib.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    with pytest.raises(files.FileUploadError, match="decompression limit"):
        _extract(buffer.getvalue(), files.DOCX_MIME)


def test_extract_docx_requires_document_xml() -> None:
    import zipfile as zipfile_lib

    buffer = io.BytesIO()
    with zipfile_lib.ZipFile(buffer, "w", zipfile_lib.ZIP_DEFLATED) as archive:
        archive.writestr("other.xml", "<nothing/>")
    with pytest.raises(files.FileUploadError, match="word/document.xml missing"):
        _extract(buffer.getvalue(), files.DOCX_MIME)


# ---------------------------------------------------------------------------
# Ingestion pipeline (SPEC §12)
# ---------------------------------------------------------------------------


async def test_ingest_pipeline_indexes_file(session, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    embedder = _FakeEmbedder()
    monkeypatch.setattr(files, "get_ai_provider", lambda: embedder)
    monkeypatch.setattr(
        files, "_download_telegram_file", _fake_download(b"alpha beta gamma")
    )

    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="notes.txt",
        mime_type="text/plain",
        size_bytes=16,
        telegram_file_id="tg-file-1",
    )
    await session.commit()

    job = await _claim_file_job(session, worker_id="w-test")
    await files.handle_files_ingest(session, job)
    await session.commit()

    file = await session.get(UserFile, file.id)
    assert file.state == FileState.indexed.value
    assert file.indexed_at is not None
    assert file.error is None

    chunks = (
        await session.scalars(
            select(FileChunk)
            .where(FileChunk.file_id == file.id)
            .order_by(FileChunk.position)
        )
    ).all()
    assert [c.text for c in chunks] == ["alpha beta gamma"]
    assert all(c.embedding is not None for c in chunks)
    assert all(c.user_id == user.id for c in chunks)
    # The download landed under the safe storage key, never the filename.
    assert (tmp_path / "files" / file.storage_key).read_bytes() == b"alpha beta gamma"


async def test_ingest_failure_is_visible_and_retryable(
    session, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())

    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="flaky.txt",
        mime_type="text/plain",
        size_bytes=5,
        telegram_file_id="tg-file-4",
    )
    await session.commit()

    async def _boom(telegram_file_id: str, destination: Path) -> None:
        raise RuntimeError("download blew up")

    file_id = file.id
    monkeypatch.setattr(files, "_download_telegram_file", _boom)
    job = await _claim_file_job(session, worker_id="w-test")
    job_id = job.id
    with pytest.raises(RuntimeError, match="download blew up"):
        await files.handle_files_ingest(session, job)

    # The visible failure state survived the pipeline transaction rollback.
    file = await session.get(UserFile, file_id, populate_existing=True)
    assert file.state == FileState.failed.value
    assert "download blew up" in (file.error or "")

    # Retry: the download now succeeds and the file becomes indexed. The
    # worker re-loads the job in a fresh session, so mirror that here.
    monkeypatch.setattr(
        files, "_download_telegram_file", _fake_download(b"recovered text")
    )
    job = await session.get(BackgroundJob, job_id)
    await files.handle_files_ingest(session, job)
    await session.commit()
    file = await session.get(UserFile, file_id)
    assert file.state == FileState.indexed.value
    assert file.error is None


async def test_ingest_extraction_failure_is_visible(session, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())
    # A "PDF" whose bytes are not a valid PDF: extraction must fail visibly.
    monkeypatch.setattr(files, "_download_telegram_file", _fake_download(b"not a pdf"))

    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="corrupt.pdf",
        mime_type="application/pdf",
        size_bytes=9,
        telegram_file_id="tg-file-8",
    )
    file_id = file.id
    await session.commit()

    job = await _claim_file_job(session, worker_id="w-test")
    with pytest.raises(files.FileUploadError, match="Could not read PDF"):
        await files.handle_files_ingest(session, job)

    file = await session.get(UserFile, file_id, populate_existing=True)
    assert file.state == FileState.failed.value
    assert "Could not read PDF" in (file.error or "")


async def test_ingest_fails_when_extracted_text_exceeds_limit(
    session, monkeypatch, tmp_path
) -> None:
    """A hostilely large text document fails visibly at the extraction stage
    (SPEC §21) instead of being chunked and embedded unboundedly."""
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(get_settings(), "max_extracted_text_chars", 1_000)
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())
    monkeypatch.setattr(files, "_download_telegram_file", _fake_download(b"x" * 5_000))

    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="huge.txt",
        mime_type="text/plain",
        size_bytes=5_000,
        telegram_file_id="tg-file-9",
    )
    file_id = file.id
    await session.commit()

    job = await _claim_file_job(session, worker_id="w-test")
    with pytest.raises(files.FileUploadError, match="too long"):
        await files.handle_files_ingest(session, job)

    file = await session.get(UserFile, file_id, populate_existing=True)
    assert file.state == FileState.failed.value
    assert "too long" in (file.error or "")
    assert (
        await session.scalar(select(func.count()).select_from(FileChunk))
        == 0
    )


async def test_ingest_fails_when_chunk_count_exceeds_limit(
    session, monkeypatch, tmp_path
) -> None:
    """The total number of chunks — and therefore embedding batches and chunk
    rows — is bounded per file (SPEC §21)."""
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(get_settings(), "chunk_size", 100)
    monkeypatch.setattr(get_settings(), "chunk_overlap", 0)
    monkeypatch.setattr(get_settings(), "max_chunks_per_file", 3)
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())
    text = " ".join(f"w{i:03d}" for i in range(200))  # ~1199 chars -> >3 chunks
    monkeypatch.setattr(
        files, "_download_telegram_file", _fake_download(text.encode())
    )

    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="many.txt",
        mime_type="text/plain",
        size_bytes=len(text),
        telegram_file_id="tg-file-10",
    )
    file_id = file.id
    await session.commit()

    job = await _claim_file_job(session, worker_id="w-test")
    with pytest.raises(files.FileUploadError, match="too large to index"):
        await files.handle_files_ingest(session, job)

    file = await session.get(UserFile, file_id, populate_existing=True)
    assert file.state == FileState.failed.value
    assert "too large to index" in (file.error or "")


async def test_ingest_pipeline_fast_fails_when_embedding_unconfigured(
    session, monkeypatch, tmp_path
) -> None:
    """V3 P42: a job enqueued before the deployment flips to chat-only must
    fail visibly at the start of the pipeline (no full backoff budget spent
    retrying a file that can never be embedded)."""
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())
    monkeypatch.setattr(
        files, "_download_telegram_file", _fake_download(b"late content")
    )

    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="late.txt",
        mime_type="text/plain",
        size_bytes=12,
        telegram_file_id="tg-file-p42b",
    )
    file_id = file.id
    await session.commit()

    # The deployment flips to chat-only after the job was already enqueued.
    monkeypatch.setattr(get_settings(), "embedding_api_key", None)

    job = await _claim_file_job(session, worker_id="w-test")
    with pytest.raises(files.FileUploadError, match="embedding provider"):
        await files.handle_files_ingest(session, job)

    file = await session.get(UserFile, file_id, populate_existing=True)
    assert file.state == FileState.failed.value
    assert "embedding provider" in (file.error or "")
    # The pipeline aborted before any download/chunk work.
    assert (
        await session.scalar(select(func.count()).select_from(FileChunk)) == 0
    )


async def test_ingest_is_idempotent_on_replay(session, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())
    monkeypatch.setattr(
        files, "_download_telegram_file", _fake_download(b"stable content")
    )

    user = await _user(session)
    await files.register_upload(
        session,
        user,
        original_filename="stable.txt",
        mime_type="text/plain",
        size_bytes=14,
        telegram_file_id="tg-file-5",
    )
    await session.commit()

    job = await _claim_file_job(session, worker_id="w-test")
    await files.handle_files_ingest(session, job)
    await session.commit()
    # Replay of the same (retried) job must not duplicate chunks.
    await files.handle_files_ingest(session, job)
    await session.commit()

    count = len((await session.scalars(select(FileChunk))).all())
    assert count == 1


async def test_ingest_missing_file_is_noop(session) -> None:
    job = BackgroundJob(
        type=files.FILES_INGEST_JOB_TYPE,
        payload={"file_id": 999999},
        status=JobStatus.running.value,
    )
    await files.handle_files_ingest(session, job)  # must not raise


async def test_cancelled_ingest_does_not_commit_chunks(
    session, monkeypatch, tmp_path
) -> None:
    """§5.5: a job cancelled mid-ingest must not store stale chunks."""
    from assistant.db import get_session_factory

    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())
    monkeypatch.setattr(
        files, "_download_telegram_file", _fake_download(b"cancellable text")
    )

    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="cancel.txt",
        mime_type="text/plain",
        size_bytes=15,
        telegram_file_id="tg-cancel",
    )
    await session.commit()

    job = await _claim_file_job(session, worker_id="w-test")
    job_id = job.id
    file_id = file.id

    # A concurrent API request cancels the job in a separate session.
    async with get_session_factory()() as cancel_session:
        await jobs_service.cancel_job(cancel_session, job_id)
        await cancel_session.commit()

    # The handler runs to completion but must skip the chunk-writing commit.
    await files.handle_files_ingest(session, job)
    await session.commit()

    refreshed = await session.get(UserFile, file_id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.state != FileState.indexed.value
    # Exactly zero rows: the cancelled run never committed any chunks.
    count = await session.scalar(
        select(func.count()).select_from(FileChunk).where(FileChunk.file_id == file_id)
    )
    assert count == 0


# ---------------------------------------------------------------------------
# Hybrid retrieval (SPEC §13)
# ---------------------------------------------------------------------------


async def test_retrieval_is_user_scoped_and_fuses_hybrid(session, monkeypatch) -> None:
    """Scores are RRF scores; results never cross user boundaries (SPEC §6)."""
    # The fusion demonstration needs the off-marker "coffee" chunk (cosine
    # distance 1) to survive the vector arm, so widen the relevance bound.
    monkeypatch.setattr(get_settings(), "retrieval_max_distance", 1.5)
    user = await _user(session, user_id=51)
    other = await _user(session, user_id=52)

    await _indexed_file(
        session,
        user,
        filename="quantum-notes.md",
        texts=["quantum entanglement basics"],
    )
    # No lexical overlap, but the vector list still ranks it 2nd.
    await _indexed_file(
        session, user, filename="coffee.txt", texts=["coffee brewing guide"]
    )
    # The other user has an identical "quantum" chunk that must never leak.
    await _indexed_file(
        session, other, filename="their-notes.md", texts=["quantum entanglement basics"]
    )

    results = await files.retrieve_chunks(
        session, user, "quantum entanglement", provider=_FakeEmbedder()
    )

    assert [r.file_name for r in results] == ["quantum-notes.md", "coffee.txt"]
    # Rank 1 in BOTH the lexical and vector lists -> 1/61 + 1/61.
    assert results[0].score == pytest.approx(2 / (files.RRF_K + 1))
    # Rank 2 in the vector list only -> 1/62.
    assert results[1].score == pytest.approx(1 / (files.RRF_K + 2))

    assert files.format_citations(results) == "Sources: quantum-notes.md, coffee.txt"
    assert await files.retrieve_chunks(session, user, "   ", provider=_FakeEmbedder()) == []


async def test_no_lexical_overlap_still_runs_vector_arm(session) -> None:
    """No lexical prerequisite (SPEC §17): a query with zero lexical overlap
    still embeds and can surface a semantically-nearest chunk via the vector
    arm (previously the lexical gate returned [] without any embedding call)."""
    user = await _user(session)
    # Marker 2 (no "quantum"/"coffee"), sharing no words with the query, so it
    # can only be reached by the vector arm. Same marker -> distance 0 < bound.
    await _indexed_file(
        session, user, filename="journal.md", texts=["daily habits and reflection journal"]
    )
    embedder = _FakeEmbedder()

    # "good morning" shares no words with the stored chunk (lexical empty),
    # but the vector arm embeds and returns the only candidate.
    results = await files.retrieve_chunks(session, user, "good morning", provider=embedder)

    assert [r.file_name for r in results] == ["journal.md"]
    # Vector rank 1 only (no lexical contribution) -> 1/(RRF_K+1).
    assert results[0].score == pytest.approx(1 / (files.RRF_K + 1))
    # Distance is tracked for the vector-recalled chunk.
    assert results[0].distance == pytest.approx(0.0)
    assert embedder.query_calls == ["good morning"]


async def test_offtopic_vector_candidate_dropped_by_distance_bound(session) -> None:
    """Meaningful relevance bound (SPEC §17): the nearest-but-unrelated vector
    candidate (cosine distance 1 > default 0.9) with no lexical overlap is
    dropped, while the genuinely relevant chunk is kept."""
    user = await _user(session)
    # "coffee" chunk: off-topic (marker 1) for a "quantum" query -> distance 1,
    # and it shares no words with the query (no lexical rescue).
    await _indexed_file(session, user, filename="coffee.txt", texts=["coffee brewing guide"])
    # Relevant chunk: marker 0, distance 0, and lexical overlap with "quantum".
    await _indexed_file(
        session, user, filename="quantum-notes.md", texts=["quantum entanglement basics"]
    )
    results = await files.retrieve_chunks(session, user, "quantum", provider=_FakeEmbedder())
    assert [r.file_name for r in results] == ["quantum-notes.md"]


async def test_cross_language_relevance_via_vector_arm(session) -> None:
    """Multilingual relevance (SPEC §13, §17): a query in one language retrieves
    an off-topic-free chunk in another language purely by vector closeness, with
    no lexical overlap and no shared keywords across languages."""
    user = await _user(session)
    # "café" (ES) and "кофе" (RU) share no ASCII words with the EN query
    # "coffee", but a multilingual embedding places them near it.
    await _indexed_file(
        session,
        user,
        filename="es-cafe.md",
        texts=["cómo preparar café"],
        marker_fn=_MultiLingualEmbedder._marker,
    )
    await _indexed_file(
        session,
        user,
        filename="ru-kofe.md",
        texts=["как приготовить кофе"],
        marker_fn=_MultiLingualEmbedder._marker,
    )

    results = await files.retrieve_chunks(session, user, "coffee", provider=_MultiLingualEmbedder())
    # Both chunks are semantically relevant (distance 0), no lexical match.
    assert sorted(r.file_name for r in results) == ["es-cafe.md", "ru-kofe.md"]
    for r in results:
        assert r.distance == pytest.approx(0.0)


class _RussianParaphraseEmbedder:
    """Treats RU inflections/paraphrases of one concept as a shared marker:
    "сварить кофе" and "рецепт эспрессо" share no words, so the chunk is
    reachable only through the vector arm."""

    _CONCEPT = {"кофе", "сварить", "рецепт", "эспрессо"}

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        return [_marker_vector(self._marker(t)) for t in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        return _marker_vector(self._marker(query))

    @classmethod
    def _marker(cls, text: str) -> int:
        return 1 if set(text.lower().split()) & cls._CONCEPT else 2


async def test_russian_inflection_paraphrase_retrieval(session) -> None:
    """Russian inflection/paraphrase (SPEC §13, §18): a query with zero
    word-level overlap with the stored chunk is recalled by the vector arm,
    and an unrelated RU chunk stays out."""
    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="espresso.md",
        texts=["рецепт эспрессо для дома"],
        marker_fn=_RussianParaphraseEmbedder._marker,
    )
    await _indexed_file(
        session,
        user,
        filename="meeting-notes.md",
        texts=["план совещания на неделю"],
        marker_fn=_RussianParaphraseEmbedder._marker,
    )

    # No word overlap with "рецепт эспрессо для дома" (inflection/paraphrase).
    results = await files.retrieve_chunks(
        session, user, "как сварить кофе", provider=_RussianParaphraseEmbedder()
    )
    assert [r.file_name for r in results] == ["espresso.md"]
    assert results[0].distance == pytest.approx(0.0)


async def test_no_match_on_either_arm_returns_empty(session) -> None:
    """Empty only when BOTH arms are empty (SPEC §17)."""
    user = await _user(session)
    embedder = _FakeEmbedder()
    assert await files.retrieve_chunks(session, user, "anything", provider=embedder) == []
    # The vector arm ran (embedding was requested) but found no chunks.
    assert embedder.query_calls == ["anything"]


async def test_rrf_recovers_lexical_candidate_outside_semantic_top_k(session) -> None:
    """A lexical candidate ranked outside the semantic top-K can still make
    the fused result (SPEC §6, §13).

    gamma matches the query lexically (same ts_rank as the others, but last
    by position) and is the WORST semantic match (marker 0 -> distance 1),
    so the pure vector top-3 is [alpha, beta, delta] — gamma is excluded.
    RRF with the lexical list puts gamma ahead of delta.
    """
    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="abc.md",
        texts=[
            "entanglement details alpha",
            "entanglement details beta",
            "entanglement details delta",
        ],
    )
    await _indexed_file(
        session, user, filename="gamma.md", texts=["entanglement details quantum"]
    )

    embedder = _FakeEmbedder()
    results = await files.retrieve_chunks(
        session, user, "entanglement details", provider=embedder, top_k=3
    )

    # gamma is a vector outlier (distance 1, dropped by the relevance bound)
    # recalled purely by the lexical arm; RRF keeps it alongside abc.md. The
    # three consecutive abc.md chunks (positions 0..2) now merge into a single
    # proximity span, so the fused result is [abc.md span, gamma.md].
    names = [r.file_name for r in results]
    assert "gamma.md" in names
    assert len(results) == 2
    assert names.count("abc.md") == 1
    abc = next(r for r in results if r.file_name == "abc.md")
    assert abc.position == 0 and abc.position_end == 2


async def test_embedding_outage_degrades_to_lexical_only(session) -> None:
    """An embedding outage must not break retrieval (SPEC §6, §13)."""
    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="warranty.txt",
        texts=["the warranty covers repairs for two years"],
    )

    class _DownEmbedder(_FakeEmbedder):
        async def embed_query(self, *, query: str) -> list[float]:
            raise AIProviderError("embedding server down")

    results = await files.retrieve_chunks(
        session, user, "warranty repairs", provider=_DownEmbedder()
    )

    assert len(results) == 1
    assert results[0].file_name == "warranty.txt"
    # Lexical rank 1 -> 1/(RRF_K+1).
    assert results[0].score == pytest.approx(1 / (files.RRF_K + 1))


async def test_retrieval_lexical_only_when_embedding_unconfigured(
    session, monkeypatch
) -> None:
    """V3 P42: with no embedding provider the vector arm is skipped by
    construction — lexical hits are still returned, the provider is never
    called (no network), and there is no outage to log."""
    monkeypatch.setattr(get_settings(), "embedding_api_key", None)
    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="warranty.txt",
        texts=["the warranty covers repairs for two years"],
    )

    embedder = _FakeEmbedder()
    results = await files.retrieve_chunks(
        session, user, "warranty repairs", provider=embedder
    )

    assert len(results) == 1
    assert results[0].file_name == "warranty.txt"
    # Lexical rank 1 only (no vector contribution) -> 1/(RRF_K+1).
    assert results[0].score == pytest.approx(1 / (files.RRF_K + 1))
    assert results[0].distance is None
    # The vector arm never ran: the provider was never asked to embed.
    assert embedder.query_calls == []


async def test_lexical_or_style_partial_overlap_recalls(session) -> None:
    """P19 (SPEC §13): lexical matching is OR-style and normalized — a chunk
    matching only ONE of the query's words is recalled, where the previous
    AND (plaintext) gate required every word to be present."""
    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="warranty.txt",
        texts=["the warranty covers repairs for two years"],
    )

    class _DownEmbedder(_FakeEmbedder):
        async def embed_query(self, *, query: str) -> list[float]:
            raise AIProviderError("embedding server down")  # force lexical-only

    # "shipping" matches nothing, but "warranty" does -> the chunk is recalled.
    results = await files.retrieve_chunks(
        session, user, "warranty shipping", provider=_DownEmbedder()
    )
    assert [r.file_name for r in results] == ["warranty.txt"]


async def test_lexical_query_is_operator_safe(session) -> None:
    """P19 (SPEC §13): operator characters in the query are normalized away, so
    arbitrary user input cannot be interpreted as a tsquery operator and the
    call never raises."""
    user = await _user(session)
    await _indexed_file(
        session, user, filename="notes.md", texts=["quantum entanglement basics"]
    )

    class _DownEmbedder(_FakeEmbedder):
        async def embed_query(self, *, query: str) -> list[float]:
            raise AIProviderError("embedding server down")  # force lexical-only

    # tsquery operators (!, |, &, <, >, parentheses) are stripped; "quantum"
    # still matches the chunk.
    for query in ("quantum & !everything", "quantum | (weird) <terms>"):
        results = await files.retrieve_chunks(session, user, query, provider=_DownEmbedder())
        assert [r.file_name for r in results] == ["notes.md"]


async def test_retrieval_merges_adjacent_chunks(session) -> None:
    """Consecutive positions of one file collapse into a bounded excerpt."""
    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="guide.md",
        texts=["quantum entanglement part one", "further quantum entanglement notes"],
    )

    results = await files.retrieve_chunks(session, user, "quantum entanglement", provider=_FakeEmbedder())

    assert len(results) == 1
    assert "part one" in results[0].text and "further" in results[0].text
    assert len(results[0].text) <= files.MERGED_TEXT_LIMIT
    # The merged excerpt spans chunks 0..1, so the citation shows the range.
    assert results[0].position == 0 and results[0].position_end == 1
    assert files.format_citations(results) == "Sources: guide.md (parts 1–2)"


def test_merge_adjacent_is_independent_of_score_order() -> None:
    """P20 (SPEC §13): same-file consecutive chunks merge even when a higher
    scoring chunk from another file sits between them in score order."""
    chunks = [
        files.RetrievedChunk(file_id=2, file_name="b.md", position=0, text="b", score=0.9),
        files.RetrievedChunk(file_id=1, file_name="a.md", position=0, text="a0", score=0.5),
        files.RetrievedChunk(file_id=1, file_name="a.md", position=1, text="a1", score=0.4),
    ]
    merged = files._merge_adjacent(chunks)
    assert len(merged) == 2
    a = next(c for c in merged if c.file_id == 1)
    assert "a0" in a.text and "a1" in a.text
    assert a.position == 0 and a.position_end == 1
    # The higher-scoring b.md span comes first.
    assert merged[0].file_id == 2


def test_merge_adjacent_uses_configurable_gap(monkeypatch) -> None:
    """P20 (SPEC §13): chunks bridge only within retrieval_merge_gap steps;
    a wider gap merges across a small run of un-retrieved chunks."""
    chunks = [
        files.RetrievedChunk(file_id=1, file_name="a.md", position=0, text="a0", score=0.5),
        files.RetrievedChunk(file_id=1, file_name="a.md", position=2, text="a2", score=0.4),
    ]
    # Default gap=1: positions 0 and 2 (a gap of 2) are NOT merged.
    assert len(files._merge_adjacent(chunks)) == 2
    # Widen the gap to 2: the missing position 1 is bridged into one span.
    monkeypatch.setattr(get_settings(), "retrieval_merge_gap", 2)
    merged = files._merge_adjacent(chunks)
    assert len(merged) == 1
    assert merged[0].position == 0 and merged[0].position_end == 2
    assert "a0" in merged[0].text and "a2" in merged[0].text


def test_citations_show_position_range_for_merged_span() -> None:
    """P20 (SPEC §13): citations reflect file AND position proximity — a merged
    span is annotated with its contiguous 1-based part range, a single chunk is
    not."""
    chunks = [
        files.RetrievedChunk(
            file_id=1, file_name="a.md", position=1, text="x", score=0.5, position_end=2
        ),
        files.RetrievedChunk(file_id=2, file_name="b.md", position=0, text="y", score=0.4),
    ]
    assert files.format_citations(chunks) == "Sources: a.md (parts 2–3), b.md"


async def test_delete_file_removes_rows_job_and_disk(session, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    user = await _user(session)
    file = await files.register_upload(
        session,
        user,
        original_filename="doomed.txt",
        mime_type="text/plain",
        size_bytes=3,
        telegram_file_id="tg-file-6",
    )
    (tmp_path / "files").mkdir(parents=True, exist_ok=True)
    disk = tmp_path / "files" / file.storage_key
    disk.write_text("data")
    await session.commit()

    storage_key = await files.delete_file(session, user, file.id)
    assert storage_key == file.storage_key
    # The disk artifact survives until the commit is confirmed (P22): a
    # failed commit must not destroy the only copy of a local upload.
    assert disk.exists()
    await session.commit()

    assert await session.get(UserFile, file.id) is None
    assert (await session.scalars(select(FileChunk))).all() == []
    job = await session.get(BackgroundJob, file.job_id)
    assert job.status == JobStatus.cancelled.value
    files.discard_storage(storage_key)
    assert not disk.exists()
    assert await files.delete_file(session, user, file.id) is None


async def test_delete_file_scoped_to_owner(session) -> None:
    user = await _user(session, user_id=51)
    other = await _user(session, user_id=52)
    file = await files.register_upload(
        session,
        user,
        original_filename="mine.txt",
        mime_type="text/plain",
        size_bytes=4,
        telegram_file_id="tg-file-7",
    )
    await session.commit()
    # The other user cannot see or delete the file.
    assert await files.get_file(session, other, file.id) is None
    assert await files.delete_file(session, other, file.id) is None
    assert await session.get(UserFile, file.id) is not None


# ---------------------------------------------------------------------------
# Lifecycle consistency: terminal-failure artifact reaping (P22)
# ---------------------------------------------------------------------------


async def _fail_terminal(session: AsyncSession, file: UserFile) -> None:
    """Mark a file's ingest job terminally failed and the file state failed."""
    job = await session.get(BackgroundJob, file.job_id)
    job.status = JobStatus.failed.value
    file.state = FileState.failed.value
    file.error = "ingestion failed"
    await session.commit()


async def test_reap_terminal_artifacts_removes_resourcable_only(
    session, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())
    user = await _user(session)

    # (a) Telegram file, terminally failed: bytes are re-downloadable -> reap.
    resourcable = await files.register_upload(
        session, user, original_filename="a.txt", mime_type="text/plain",
        size_bytes=3, telegram_file_id="tg-file-a",
    )
    # (b) Local upload, terminally failed: disk is the ONLY source -> keep.
    local = await files.register_local_upload(
        session, user, original_filename="b.txt", mime_type="text/plain",
        data=b"local",
    )
    # (c) Telegram file mid-backoff (job pending): retry will re-download ->
    # keep the current artifact so it is not deleted under a live retry.
    backoff = await files.register_upload(
        session, user, original_filename="c.txt", mime_type="text/plain",
        size_bytes=3, telegram_file_id="tg-file-c",
    )
    await session.commit()
    (tmp_path / "files").mkdir(parents=True, exist_ok=True)
    for f in (resourcable, backoff):
        (tmp_path / "files" / f.storage_key).write_text("data")
    await _fail_terminal(session, resourcable)
    await _fail_terminal(session, local)
    backoff.state = FileState.failed.value
    backoff.error = "transient"
    await session.commit()  # backoff job stays pending (job_max_attempts not hit)

    reaped = await files.reap_terminal_artifacts(session)

    assert reaped == 1
    assert not (tmp_path / "files" / resourcable.storage_key).exists()
    assert (tmp_path / "files" / local.storage_key).exists()
    assert (tmp_path / "files" / backoff.storage_key).exists()
    # Idempotent: nothing left to reap.
    assert await files.reap_terminal_artifacts(session) == 0


async def test_local_upload_missing_artifact_fails_visibly(
    session, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())

    user = await _user(session)
    file = await files.register_local_upload(
        session, user, original_filename="gone.txt", mime_type="text/plain",
        data=b"payload",
    )
    file_id = file.id
    await session.commit()
    (tmp_path / "files" / file.storage_key).unlink()  # simulate lost volume

    job = await _claim_file_job(session, worker_id="w-test")
    with pytest.raises(files.FileUploadError, match="missing and cannot be re-ingested"):
        await files.handle_files_ingest(session, job)

    file = await session.get(UserFile, file_id, populate_existing=True)
    assert file.state == FileState.failed.value
    assert "missing and cannot be re-ingested" in (file.error or "")


# ---------------------------------------------------------------------------
# Bot wiring: document upload
# ---------------------------------------------------------------------------


def _fake_document_message(mime_type: str = "text/plain") -> SimpleNamespace:
    document = SimpleNamespace(
        file_name="notes.txt",
        mime_type=mime_type,
        file_size=16,
        file_id="tg-file-9",
        file_unique_id="tg-uniq-9",
    )
    return SimpleNamespace(
        document=document,
        from_user=SimpleNamespace(
            id=51, first_name="Doc", last_name=None, username=None, is_bot=False
        ),
        answer=AsyncMock(),
    )


async def test_on_document_registers_and_confirms(session) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.commit()

    message = _fake_document_message()
    await on_document(message, session)
    await session.commit()

    message.answer.assert_awaited_once()
    assert "Saved notes.txt" in message.answer.await_args.args[0]
    file = (await session.scalars(select(UserFile))).one()
    assert file.state == FileState.queued.value
    assert file.original_filename == "notes.txt"
    assert file.job_id is not None


async def test_on_document_reports_rejection(session) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.commit()

    message = _fake_document_message(mime_type="image/png")
    await on_document(message, session)
    await session.commit()

    message.answer.assert_awaited_once()
    assert "Couldn't store" in message.answer.await_args.args[0]
    file = (await session.scalars(select(UserFile))).one()
    assert file.state == FileState.rejected.value
