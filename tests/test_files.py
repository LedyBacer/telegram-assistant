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


def test_extract_text_plain_markdown_and_docx() -> None:
    assert files.extract_text("héllo\n".encode(), "text/plain") == "héllo\n"
    assert files.extract_text(b"# md", "text/markdown") == "# md"

    import docx as docx_lib

    buffer = io.BytesIO()
    document = docx_lib.Document()
    document.add_paragraph("Hello docx world")
    document.save(buffer)
    assert files.extract_text(buffer.getvalue(), files.DOCX_MIME) == "Hello docx world"


def test_extract_text_unsupported_raises() -> None:
    with pytest.raises(files.FileUploadError, match="Unsupported"):
        files.extract_text(b"x", "image/png")


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

    # Vector-only top-3 would be exactly the abc.md chunks (all distance 0);
    # gamma (vector rank 4) is recovered by the lexical list into the fused
    # top-3 (gamma >= 1/62 + 1/64 beats delta = 1/63 + 1/64).
    names = [r.file_name for r in results]
    assert "gamma.md" in names
    assert len(results) == 3
    assert names.count("abc.md") == 2


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

    assert await files.delete_file(session, user, file.id) is True
    await session.commit()

    assert await session.get(UserFile, file.id) is None
    assert (await session.scalars(select(FileChunk))).all() == []
    job = await session.get(BackgroundJob, file.job_id)
    assert job.status == JobStatus.cancelled.value
    assert not disk.exists()
    assert await files.delete_file(session, user, file.id) is False


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
    assert await files.delete_file(session, other, file.id) is False
    assert await session.get(UserFile, file.id) is not None


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
