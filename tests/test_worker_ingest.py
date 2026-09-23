"""Worker transaction-ownership regression tests (SPEC §5, V3 §1).

These run a REAL ``files.ingest`` job through ``JobWorker._run_job()`` — not
by calling the file handler directly — to prove the new transaction contract:

* the worker owns job claiming and the final job state;
* the file handler owns its domain transactions and commits between external
  I/O stages (download → extract → embed → chunk-write);
* the multiple internal commits do not break the worker;
* the owner token / lease still protects completion;
* a cancellation during ingestion cannot commit stale chunks.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from assistant.config import get_settings
from assistant.models.files import FileChunk, FileState
from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.models.users import User
from assistant.services import files
from assistant.services import jobs as jobs_service
from assistant.services.users import upsert_user
from assistant.worker.main import JobWorker


async def _user(session: AsyncSession, user_id: int = 71) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="W")
    await session.commit()
    return user


def _worker(engine) -> JobWorker:
    """A JobWorker whose sessions come from the test engine."""
    worker = JobWorker(worker_id="w-test")
    worker._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return worker


class _FakeEmbedder:
    def __init__(
        self,
        *,
        reached: asyncio.Event | None = None,
        cancel_done: asyncio.Event | None = None,
    ) -> None:
        self.reached = reached
        # When set, the fake blocks here (in the middle of the embedding
        # stage) until the caller has committed a job cancel — this makes the
        # "cancel mid-ingest" test deterministic instead of racing.
        self.cancel_done = cancel_done
        self.document_calls: list[list[str]] = []

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        self.document_calls.append(list(texts))
        if self.reached is not None:
            self.reached.set()  # signal: pipeline has reached the embedding stage
        if self.cancel_done is not None:
            await self.cancel_done.wait()
        from assistant.models.files import EMBEDDING_DIMENSIONS

        vec = [0.0] * EMBEDDING_DIMENSIONS
        vec[0] = 1.0
        return [vec for _ in texts]


async def _seed_local_file(
    session: AsyncSession, user: User, text: str
) -> files.UserFile:
    return await files.register_local_upload(
        session,
        user,
        original_filename="notes.txt",
        mime_type="text/plain",
        data=text.encode("utf-8"),
    )


# ---------------------------------------------------------------------------
# Happy path: real JobWorker._run_job over a files.ingest job
# ---------------------------------------------------------------------------


async def test_run_job_ingests_file_end_to_end(session, engine, monkeypatch, tmp_path) -> None:
    """The worker drives the whole pipeline; the job ends ``completed``."""
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())

    user = await _user(session)
    file = await _seed_local_file(session, user, "alpha beta gamma delta")
    await session.commit()

    worker = _worker(engine)

    # Claim the job to obtain a real owner token (the worker's poll does this
    # in production; here we mirror it).
    factory = worker._session_factory
    async with factory() as s:
        jobs = await jobs_service.claim_jobs(s, worker_id="w-test", limit=1)
        await s.commit()
    assert len(jobs) == 1
    job = jobs[0]

    await worker._run_job(job.id, job.type, job.locked_by)

    # Capture scalar ids before expiring (reading an expired ORM attribute to
    # build a WHERE clause triggers a synchronous lazy refresh -> MissingGreenlet).
    file_id = file.id
    user_id = user.id

    # The worker committed in a separate connection; expire the fixture
    # session's identity map so the reads below hit the database, not the
    # stale in-memory objects seeded above.
    session.expire_all()

    # The file reached ``indexed`` and the chunk persisted.
    file = await session.scalar(select(files.UserFile).where(files.UserFile.id == file_id))
    assert file is not None
    assert file.state == FileState.indexed.value
    assert file.indexed_at is not None
    assert file.error is None

    chunks = (
        (
            await session.scalars(
                select(FileChunk).where(FileChunk.file_id == file_id)
            )
        )
        .all()
    )
    assert len(chunks) == 1
    assert all(c.embedding is not None for c in chunks)
    assert all(c.user_id == user_id for c in chunks)

    # The background job reached ``completed``; lease fields are cleared.
    job_row = await session.get(BackgroundJob, job.id)
    assert job_row.status == JobStatus.completed.value
    assert job_row.locked_by is None
    assert job_row.lease_until is None
    assert job_row.last_error is None


async def test_run_job_stale_owner_cannot_complete(session, engine, monkeypatch, tmp_path) -> None:
    """A foreign owner token cannot mark the job completed (lease protection)."""
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())

    user = await _user(session)
    await _seed_local_file(session, user, "lease protected")
    await session.commit()

    worker = _worker(engine)
    factory = worker._session_factory
    async with factory() as s:
        jobs = await jobs_service.claim_jobs(s, worker_id="w-test", limit=1)
        await s.commit()
    job = jobs[0]
    real_owner = job.locked_by
    assert real_owner is not None

    # A stale / foreign owner token runs the same job id.
    await worker._run_job(job.id, job.type, f"{real_owner}-stale")

    # The handler's work may have committed, but the foreign token must NOT
    # have been able to mark the job completed: the lease still belongs to
    # the real owner, so the job is still ``running``.
    job_row = await session.get(BackgroundJob, job.id)
    assert job_row.status == JobStatus.running.value
    assert job_row.locked_by == real_owner

    # The real owner can now complete it.
    await worker._run_job(job.id, job.type, real_owner)
    job_row = await session.get(BackgroundJob, job.id, populate_existing=True)
    assert job_row.status == JobStatus.completed.value


async def test_run_job_cancelled_mid_ingest_commits_no_chunks(
    session, engine, monkeypatch, tmp_path
) -> None:
    """§5.5 / V3 §1: cancellation during ingestion cannot commit stale chunks."""
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    reached = asyncio.Event()
    cancel_done = asyncio.Event()
    monkeypatch.setattr(
        files, "get_ai_provider", lambda: _FakeEmbedder(reached=reached, cancel_done=cancel_done)
    )

    user = await _user(session)
    file = await _seed_local_file(session, user, "cancellable content here")
    file_id = file.id
    await session.commit()

    worker = _worker(engine)
    factory = worker._session_factory
    async with factory() as s:
        jobs = await jobs_service.claim_jobs(s, worker_id="w-test", limit=1)
        await s.commit()
    job = jobs[0]

    task = asyncio.create_task(worker._run_job(job.id, job.type, job.locked_by))
    # Wait until the pipeline has passed download/extract and reached the
    # embedding stage, then cancel the job in a separate session (a concurrent
    # API request).
    await asyncio.wait_for(reached.wait(), timeout=10)
    # The pipeline is blocked in the embedding stage. Commit the cancel in a
    # separate session (a concurrent API request), then release the fake
    # embedder so the pipeline reaches its cancellation check AFTER the cancel
    # is committed.
    async with factory() as cancel_session:
        await jobs_service.cancel_job(cancel_session, job.id)
        await cancel_session.commit()
    cancel_done.set()
    await asyncio.wait_for(task, timeout=10)

    # Expire the identity map so the reads hit the database.
    session.expire_all()

    # Zero chunks: the cancelled run never committed the chunk-write.
    count = await session.scalar(
        select(func.count()).select_from(FileChunk).where(FileChunk.file_id == file_id)
    )
    assert count == 0

    # The file is not indexed (it is stuck in a pre-index state).
    file_row = await session.scalar(select(files.UserFile).where(files.UserFile.id == file_id))
    assert file_row is not None
    assert file_row.state != FileState.indexed.value

    # The job remains cancelled; the foreign completion attempt was refused.
    job_row = await session.get(BackgroundJob, job.id)
    assert job_row.status == JobStatus.cancelled.value
