"""Durable PostgreSQL job queue (SPEC §9).

All claim paths use ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a single
explicit transaction, so concurrent workers can never double-claim a job.

States: pending -> running -> completed | failed.
``pending`` jobs become claimable when ``available_at <= now()``; failing a
job that has attempts remaining re-queues it with exponential backoff.
Running jobs whose ``locked_at`` is older than the TTL are recovered back to
pending so a dead worker cannot wedge the queue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.jobs import BackgroundJob, JobStatus

# Base delay for exponential backoff: attempt N retries after base * 2^(N-1)
BACKOFF_BASE_SECONDS = 30

_CLAIM_SQL = text(
    """
    UPDATE background_jobs
    SET status = 'running',
        locked_at = now(),
        locked_by = :worker_id
    WHERE id IN (
        SELECT id FROM background_jobs
        WHERE status = 'pending'
          AND available_at <= now()
        ORDER BY available_at, id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id
    """
)


async def create_job(
    session: AsyncSession,
    *,
    type: str,
    payload: dict[str, Any] | None = None,
    user_id: int | None = None,
    idempotency_key: str | None = None,
    available_at: datetime | None = None,
    max_attempts: int = 3,
    extra: dict[str, Any] | None = None,
) -> BackgroundJob:
    """Insert a pending job. With an ``idempotency_key`` the insert is
    idempotent: a concurrent/previous insert with the same key returns the
    existing job instead of raising."""
    job = BackgroundJob(
        type=type,
        payload=payload or {},
        user_id=user_id,
        idempotency_key=idempotency_key,
        available_at=available_at,
        max_attempts=max_attempts,
        extra=extra or {},
    )
    session.add(job)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        if idempotency_key is None:
            raise
        existing = await session.scalar(
            select(BackgroundJob).where(BackgroundJob.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise
        return existing
    return job


async def claim_jobs(
    session: AsyncSession, *, worker_id: str, limit: int = 1
) -> list[BackgroundJob]:
    """Atomically claim up to ``limit`` claimable jobs for ``worker_id``.

    The UPDATE + SKIP LOCKED sub-select runs as one statement, so two
    concurrent claimers can never receive the same job id.
    """
    rows = (await session.execute(_CLAIM_SQL, {"worker_id": worker_id, "limit": limit})).all()
    if not rows:
        return []
    jobs = (
        await session.scalars(
            select(BackgroundJob).where(BackgroundJob.id.in_([r.id for r in rows]))
        )
    ).all()
    return list(jobs)


async def claim_job(
    session: AsyncSession, *, worker_id: str
) -> BackgroundJob | None:
    """Claim a single claimable job, if any."""
    jobs = await claim_jobs(session, worker_id=worker_id, limit=1)
    return jobs[0] if jobs else None


async def complete_job(
    session: AsyncSession, job_id: int, *, worker_id: str
) -> BackgroundJob:
    """Mark a running job completed. Only the claiming worker can do this."""
    return await _transition(
        session,
        job_id,
        worker_id=worker_id,
        updates={
            "status": JobStatus.completed.value,
            "completed_at": datetime.now(UTC),
            "locked_at": None,
            "locked_by": None,
            "last_error": None,
        },
    )


async def fail_job(
    session: AsyncSession, job_id: int, *, worker_id: str, error: str
) -> BackgroundJob:
    """Record a failed attempt. If attempts remain, re-queue with
    exponential backoff (30s, 60s, 120s, ...); otherwise mark failed."""
    job = (
        await session.scalar(
            select(BackgroundJob)
            .where(BackgroundJob.id == job_id, BackgroundJob.locked_by == worker_id)
            .with_for_update()
        )
    )
    if job is None or job.status != JobStatus.running.value:
        raise ValueError(f"Job {job_id} is not running under worker {worker_id!r}")
    job.attempts += 1
    job.last_error = error[:2000]
    if job.attempts < job.max_attempts:
        delay = timedelta(seconds=BACKOFF_BASE_SECONDS * (2 ** (job.attempts - 1)))
        job.status = JobStatus.pending.value
        job.available_at = datetime.now(UTC) + delay
        job.locked_at = None
        job.locked_by = None
    else:
        job.status = JobStatus.failed.value
        job.locked_at = None
        job.locked_by = None
    await session.flush()
    return job


async def cancel_job(session: AsyncSession, job_id: int) -> BackgroundJob:
    """Cancel a pending or running job. Terminal states are not cancellable."""
    job = (
        await session.scalar(
            select(BackgroundJob).where(BackgroundJob.id == job_id).with_for_update()
        )
    )
    if job is None:
        raise ValueError(f"Job {job_id} does not exist")
    if job.status in (JobStatus.completed.value, JobStatus.failed.value, JobStatus.cancelled.value):
        return job
    job.status = JobStatus.cancelled.value
    job.locked_at = None
    job.locked_by = None
    await session.flush()
    return job


async def recover_abandoned_locks(session: AsyncSession, *, ttl_seconds: float) -> int:
    """Re-queue running jobs whose lock is older than ``ttl_seconds``.
    Returns the number of recovered jobs."""
    result = await session.execute(
        text(
            """
            UPDATE background_jobs
            SET status = 'pending',
                locked_at = NULL,
                locked_by = NULL,
                available_at = now()
            WHERE status = 'running'
              AND locked_at < now() - make_interval(secs => :ttl)
            """
        ),
        {"ttl": ttl_seconds},
    )
    return result.rowcount or 0


async def _transition(
    session: AsyncSession,
    job_id: int,
    *,
    worker_id: str,
    updates: dict[str, Any],
) -> BackgroundJob:
    job = (
        await session.scalar(
            select(BackgroundJob)
            .where(BackgroundJob.id == job_id, BackgroundJob.locked_by == worker_id)
            .with_for_update()
        )
    )
    if job is None or job.status != JobStatus.running.value:
        raise ValueError(f"Job {job_id} is not running under worker {worker_id!r}")
    for column, value in updates.items():
        setattr(job, column, value)
    await session.flush()
    return job
