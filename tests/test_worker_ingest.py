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

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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


# ---------------------------------------------------------------------------
# V4 §6/§7/§8: lease-lost supervision of long handlers
# ---------------------------------------------------------------------------


class _LostLease:
    """A minimal stand-in for the worker's ``JobLease`` with a lost lease."""

    def __init__(self) -> None:
        self._lost = asyncio.Event()
        self._lost.set()

    @property
    def lost(self) -> asyncio.Event:
        return self._lost

    def raise_if_lost(self) -> None:
        from assistant.services.jobs import LeaseLostError

        raise LeaseLostError("lease lost")


async def test_stale_lease_commits_no_chunks(session, engine, monkeypatch, tmp_path) -> None:
    """V4 §6: a lease lost before the final chunk commit must not commit chunks.

    The pipeline runs to completion (fake embedder succeeds) but the lease is
    reported lost at the pre-commit guard, so the handler rolls back and the
    file stays un-indexed with no chunk rows.
    """
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    monkeypatch.setattr(files, "get_ai_provider", lambda: _FakeEmbedder())

    user = await _user(session)
    file = await _seed_local_file(session, user, "stale lease content")
    file_id = file.id
    await session.commit()

    job_row = (
        await session.scalar(
            select(BackgroundJob).where(BackgroundJob.type == files.FILES_INGEST_JOB_TYPE)
        )
    )
    assert job_row is not None

    token = jobs_service.current_lease.set(_LostLease())
    try:
        await files._run_pipeline(session, file, job_row)
    finally:
        jobs_service.current_lease.reset(token)

    session.expire_all()
    count = await session.scalar(
        select(func.count()).select_from(FileChunk).where(FileChunk.file_id == file_id)
    )
    assert count == 0
    file_row = await session.scalar(select(files.UserFile).where(files.UserFile.id == file_id))
    assert file_row is not None
    assert file_row.state != FileState.indexed.value
    # No failure was stamped either — this is a lease loss, not a real failure.
    assert file_row.state != FileState.failed.value


async def test_lease_lost_failure_not_recorded(session, engine, monkeypatch, tmp_path) -> None:
    """V4 §7: a lease-lost old owner must not mark the file ``failed``.

    The pipeline raises a genuine error, but the lease is already lost (a new
    owner is re-ingesting), so the handler must skip ``_record_failure`` and
    leave the file state to the new owner.
    """
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))

    user = await _user(session)
    file = await _seed_local_file(session, user, "failure after lease loss")
    file_id = file.id
    await session.commit()

    job_row = (
        await session.scalar(
            select(BackgroundJob).where(BackgroundJob.type == files.FILES_INGEST_JOB_TYPE)
        )
    )
    assert job_row is not None

    async def _boom(_session, _file, _job) -> None:
        raise RuntimeError("pipeline blew up")

    monkeypatch.setattr(files, "_run_pipeline", _boom)

    token = jobs_service.current_lease.set(_LostLease())
    try:
        with pytest.raises(RuntimeError, match="pipeline blew up"):
            await files.handle_files_ingest(session, job_row)
    finally:
        jobs_service.current_lease.reset(token)

    session.expire_all()
    file_row = await session.scalar(select(files.UserFile).where(files.UserFile.id == file_id))
    assert file_row is not None
    # A genuine failure (no lease loss) WOULD mark it failed — this one must not.
    assert file_row.state != FileState.failed.value


async def test_record_failure_atomic_with_concurrent_reclaim(
    session: AsyncSession, engine: AsyncEngine, monkeypatch, tmp_path
) -> None:
    """V5.1: ``_record_failure`` re-validates ownership WITH a row lock in the
    same transaction as the failed-state write.

    A concurrent recovery + re-claim by a new owner takes the same row lock,
    so it cannot land between the ownership check and the commit: if the
    re-claim is committed first, the stale owner's row-locked recheck sees it
    and skips; if the failure transaction holds the lock, the re-claim waits
    behind it and the failure stamp is the authoritative state. Either way a
    stale failure can never overwrite a new owner's committed work.
    """
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))

    user = await _user(session)
    file = await _seed_local_file(session, user, "failure after reclaim")
    file_id = file.id
    await session.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)

    # A claims the job (its real owner token).
    async with factory() as a, a.begin():
        job = (
            await a.scalar(
                select(BackgroundJob).where(BackgroundJob.type == files.FILES_INGEST_JOB_TYPE)
            )
        )
        claimed = await jobs_service.claim_job(a, worker_id="A")
    assert job is not None and claimed is not None
    job_id, token_a = job.id, claimed.locked_by

    # B expires A's lease, recovers, re-claims and completes the job — all
    # committed from separate connections (the real recovery path).
    async with factory() as b, b.begin():
        await b.execute(
            text(
                "UPDATE background_jobs SET lease_until = now() - interval '1 minute' "
                "WHERE id=:id"
            ),
            {"id": job_id},
        )
        assert await jobs_service.recover_abandoned(b) == 1
        await b.execute(
            text("UPDATE background_jobs SET available_at = now() WHERE status = 'pending'")
        )
    async with factory() as b2, b2.begin():
        claimed_b = await jobs_service.claim_job(b2, worker_id="B")
        assert claimed_b is not None and claimed_b.id == job_id
        done = await jobs_service.complete_job(b2, job_id, owner_token=claimed_b.locked_by)
        assert done.status == JobStatus.completed.value

    # A's pipeline now fails and records the visible failure state. The
    # row-locked recheck must see B's committed completion and skip.
    await files._record_failure(file_id, job_id, token_a, "stale failure")

    session.expire_all()
    file_row = await session.scalar(
        select(files.UserFile).where(files.UserFile.id == file_id)
    )
    assert file_row is not None
    # A stale failure must not clobber the file the new owner took over.
    assert file_row.state != FileState.failed.value
    # B's committed completion is intact.
    job_row = await session.scalar(
        select(BackgroundJob)
        .where(BackgroundJob.id == job_id)
        .execution_options(populate_existing=True)
    )
    assert job_row is not None
    assert job_row.status == JobStatus.completed.value


async def test_record_failure_stamps_when_owned(
    session: AsyncSession, engine: AsyncEngine, monkeypatch, tmp_path
) -> None:
    """Positive control: a live owner's ``_record_failure`` stamps the file
    ``failed`` in the same row-locked transaction (the lock does not
    over-block legitimate owners)."""
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))

    user = await _user(session)
    file = await _seed_local_file(session, user, "genuine failure")
    file_id = file.id
    await session.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as a, a.begin():
        job = (
            await a.scalar(
                select(BackgroundJob).where(BackgroundJob.type == files.FILES_INGEST_JOB_TYPE)
            )
        )
        claimed = await jobs_service.claim_job(a, worker_id="A")
    assert job is not None and claimed is not None
    job_id, token_a = job.id, claimed.locked_by

    await files._record_failure(file_id, job_id, token_a, "embedding blew up")

    session.expire_all()
    file_row = await session.scalar(
        select(files.UserFile).where(files.UserFile.id == file_id)
    )
    assert file_row is not None
    assert file_row.state == FileState.failed.value
    assert file_row.error == "embedding blew up"


async def test_lease_heartbeat_marks_lost_on_renewal_error(monkeypatch) -> None:
    """V4 §8: a renewal that raises is treated as a lost lease.

    The heartbeat must exit promptly (not hang out the cadence) and mark the
    lease lost so the worker cancels the in-flight handler.
    """
    from assistant.worker.main import JobLease

    class _BoomFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *exc) -> bool:
            return False

    monkeypatch.setattr(jobs_service, "LEASE_SECONDS", 0.05)
    lease = JobLease(_BoomFactory(), 1, "owner-token")
    await asyncio.wait_for(lease.heartbeat(), timeout=5)
    assert lease.lost.is_set()
    try:
        lease.raise_if_lost()
        raise AssertionError("expected LeaseLostError")
    except jobs_service.LeaseLostError:
        pass


async def test_run_job_cancels_handler_on_lost_lease(session, engine, monkeypatch, tmp_path) -> None:
    """V4 §8: the worker cancels the handler the moment the lease is lost.

    The embedder blocks; we expire the lease so the (fast) heartbeat's renewal
    fails, the worker cancels the handler, and no chunks are committed.
    """
    monkeypatch.setattr(get_settings(), "file_storage_dir", str(tmp_path / "files"))
    reached = asyncio.Event()
    block = asyncio.Event()  # never set: the handler blocks in embed until cancelled
    monkeypatch.setattr(
        files, "get_ai_provider", lambda: _FakeEmbedder(reached=reached, cancel_done=block)
    )

    user = await _user(session)
    file = await _seed_local_file(session, user, "cancel on lost lease")
    file_id = file.id
    await session.commit()

    worker = _worker(engine)
    factory = worker._session_factory
    async with factory() as s:
        jobs = await jobs_service.claim_jobs(s, worker_id="w-test", limit=1)
        await s.commit()
    job = jobs[0]

    # Speed up the heartbeat cadence so a lost lease is detected within the
    # test window.
    monkeypatch.setattr(jobs_service, "LEASE_SECONDS", 0.1)

    task = asyncio.create_task(worker._run_job(job.id, job.type, job.locked_by))
    # Deterministic: wait until the handler is blocked in the embedding stage
    # (a valid 120s lease is still live, so the heartbeat is a no-op), THEN
    # expire the lease so the next heartbeat renewal finds nothing to renew.
    await asyncio.wait_for(reached.wait(), timeout=10)
    from sqlalchemy import text

    async with factory() as s:
        await s.execute(
            text("UPDATE background_jobs SET lease_until = now() - interval '1 second'"),
        )
        await s.commit()
    # The fast heartbeat finds the expired lease, marks it lost, and cancels
    # the handler; _run_job must return (not hang) once the lease is lost.
    await asyncio.wait_for(task, timeout=10)

    session.expire_all()
    count = await session.scalar(
        select(func.count()).select_from(FileChunk).where(FileChunk.file_id == file_id)
    )
    assert count == 0
    file_row = await session.scalar(select(files.UserFile).where(files.UserFile.id == file_id))
    assert file_row is not None
    assert file_row.state not in (FileState.indexed.value, FileState.failed.value)
