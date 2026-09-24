"""Durable PostgreSQL job queue (SPEC §9) with real leases (SPEC §5.2).

All claim paths use ``SELECT ... FOR UPDATE SKIP LOCKED`` inside a single
explicit transaction, so concurrent workers can never double-claim a job.

States: pending -> running -> completed | failed | cancelled.

Leases (not a fixed "lock older than N minutes" TTL):
  * a claim sets ``locked_by`` (the owner token) and ``lease_until``;
  * the owner renews the lease with a heartbeat while the job runs;
  * a running job is abandoned ONLY when ``lease_until < now()``;
  * only the current owner (and only while the lease is live) may
    complete / fail / renew the job;
  * crash / abandoned recovery consumes the bounded retry budget, so a job
    that keeps dying can never retry forever (SPEC §5.3).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.jobs import BackgroundJob, JobStatus

# Base delay for exponential backoff: attempt N retries after base * 2^(N-1)
BACKOFF_BASE_SECONDS = 30
# How long a claimed job holds its lease before it is considered abandoned
# unless the owner renews it. Generous relative to a poll/heartbeat cadence.
LEASE_SECONDS = 120

# The lease of the job currently running in this worker task, if any (V4 §4).
# Long handlers (file ingestion) call ``current_lease().raise_if_lost()``
# before their final domain commit so an old owner can never commit side-
# effects under a stale lease. ``Any`` (not the worker's ``JobLease`` type) to
# avoid a circular import: the worker sets this, the services read it.
current_lease: ContextVar[Any] = ContextVar("current_lease", default=None)


class LeaseLostError(Exception):
    """The job's lease was lost mid-run (expired, recovered, or the renewal
    itself failed). Handlers check ``current_lease().raise_if_lost()`` before
    committing side-effects so an old owner can never commit under a stale
    lease (V4 §1/§6)."""


_CLAIM_SQL = text(
    """
    UPDATE background_jobs
    SET status = 'running',
        locked_at = now(),
        locked_by = :owner,
        lease_until = now() + make_interval(secs => :lease)
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


def new_owner_token(worker_id: str) -> str:
    """A unique lease owner token: worker identity plus a per-claim UUID so a
    stale completion from a long-dead claim cannot match a fresh one."""
    return f"{worker_id}:{uuid.uuid4().hex}"


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
    """Insert a pending job.

    With an ``idempotency_key`` the insert is idempotent and, critically,
    NEVER rolls back the caller's transaction (SPEC §5.4). It uses PostgreSQL
    ``INSERT ... ON CONFLICT DO NOTHING``: a lost uniqueness race is a
    no-op at the database level (rowcount 0), so no exception is raised and
    the caller's outer transaction stays intact. We then re-select the
    winning row and return it.
    """
    if idempotency_key is not None:
        existing = await session.scalar(
            select(BackgroundJob).where(BackgroundJob.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing

    values: dict[str, Any] = {
        "type": type,
        "payload": payload or {},
        "user_id": user_id,
        "idempotency_key": idempotency_key,
        "max_attempts": max_attempts,
        "extra": extra or {},
    }
    # Omit available_at when unset so the column's server default (now())
    # applies instead of inserting an explicit NULL.
    if available_at is not None:
        values["available_at"] = available_at

    stmt = pg_insert(BackgroundJob).values(**values).returning(BackgroundJob)
    if idempotency_key is not None:
        stmt = stmt.on_conflict_do_nothing(index_elements=["idempotency_key"])

    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        # Lost the uniqueness race: the conflicting insert was a no-op.
        row = await session.scalar(
            select(BackgroundJob).where(BackgroundJob.idempotency_key == idempotency_key)
        )
        if row is None:  # pragma: no cover - defensive
            raise RuntimeError(f"idempotent job {idempotency_key!r} vanished")
    return row


async def claim_jobs(
    session: AsyncSession, *, worker_id: str, limit: int = 1
) -> list[BackgroundJob]:
    """Atomically claim up to ``limit`` claimable jobs for ``worker_id``.

    Each claimed job gets a unique owner token (in ``locked_by``) and a lease
    in ``lease_until``. The caller reads ``job.locked_by`` as the token to
    present for later renew/complete/fail.
    """
    owner = new_owner_token(worker_id)
    rows = (
        await session.execute(
            _CLAIM_SQL, {"owner": owner, "lease": LEASE_SECONDS, "limit": limit}
        )
    ).all()
    if not rows:
        return []
    # populate_existing forces the freshly-claimed rows (already in the
    # identity map) to refresh their locked_by / lease_until from the DB,
    # which the worker relies on as its owner token.
    jobs = (
        await session.scalars(
            select(BackgroundJob)
            .where(BackgroundJob.id.in_([r.id for r in rows]))
            .execution_options(populate_existing=True)
        )
    ).all()
    return list(jobs)


async def claim_job(
    session: AsyncSession, *, worker_id: str
) -> BackgroundJob | None:
    """Claim a single claimable job, if any."""
    jobs = await claim_jobs(session, worker_id=worker_id, limit=1)
    return jobs[0] if jobs else None


async def renew_lease(
    session: AsyncSession,
    job_id: int,
    *,
    owner_token: str,
    lease_seconds: int = LEASE_SECONDS,
) -> bool:
    """Heartbeat: extend the lease for the current owner.

    Returns ``True`` if the lease was renewed (we still own a live lease),
    ``False`` if the job is no longer ours to own (completed, failed,
    cancelled, or the lease already expired and was recovered)."""
    result = await session.execute(
        text(
            """
            UPDATE background_jobs
            SET lease_until = now() + make_interval(secs => :lease)
            WHERE id = :job_id
              AND status = 'running'
              AND locked_by = :owner
              AND lease_until >= now()
            """
        ),
        {"job_id": job_id, "owner": owner_token, "lease": lease_seconds},
    )
    await session.flush()
    return (result.rowcount or 0) > 0


async def complete_job(
    session: AsyncSession, job_id: int, *, owner_token: str
) -> BackgroundJob:
    """Mark a running job completed. Only the current owner with a live lease
    may do this."""
    return await _transition(
        session,
        job_id,
        owner_token=owner_token,
        updates={
            "status": JobStatus.completed.value,
            "completed_at": datetime.now(UTC),
            "locked_at": None,
            "locked_by": None,
            "lease_until": None,
            "last_error": None,
        },
    )


async def fail_job(
    session: AsyncSession, job_id: int, *, owner_token: str, error: str
) -> BackgroundJob:
    """Record a failed attempt from the current owner. If attempts remain,
    re-queue with exponential backoff (30s, 60s, 120s, ...); otherwise mark
    failed. Only the current owner with a live lease may do this."""
    job = await _owned_running_job(session, job_id, owner_token)
    job.attempts += 1
    job.last_error = error[:2000]
    now = datetime.now(UTC)
    if job.attempts < job.max_attempts:
        delay = timedelta(seconds=BACKOFF_BASE_SECONDS * (2 ** (job.attempts - 1)))
        job.status = JobStatus.pending.value
        job.available_at = now + delay
    else:
        job.status = JobStatus.failed.value
        job.completed_at = now
    job.locked_at = None
    job.locked_by = None
    job.lease_until = None
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
    if job.status in (
        JobStatus.completed.value,
        JobStatus.failed.value,
        JobStatus.cancelled.value,
    ):
        return job
    job.status = JobStatus.cancelled.value
    job.locked_at = None
    job.locked_by = None
    job.lease_until = None
    await session.flush()
    return job


async def recover_abandoned(session: AsyncSession) -> int:
    """Re-queue running jobs whose lease has EXPIRED, consuming the bounded
    retry budget each time (SPEC §5.3). A job that keeps crashing eventually
    exhausts ``max_attempts`` and is marked failed — never retried forever.

    Implemented as one atomic UPDATE (RHS ``attempts`` is the pre-increment
    value), so it is independent of ORM identity-map freshness.

    Returns the number of jobs recovered."""
    result = await session.execute(
        text(
            """
            UPDATE background_jobs
            SET attempts = attempts + 1,
                status = CASE
                    WHEN attempts + 1 >= max_attempts THEN 'failed'
                    ELSE 'pending'
                END,
                locked_at = NULL,
                locked_by = NULL,
                lease_until = NULL,
                completed_at = CASE
                    WHEN attempts + 1 >= max_attempts THEN now()
                    ELSE completed_at
                END,
                last_error = CASE
                    WHEN attempts + 1 >= max_attempts
                        THEN 'abandoned: lease expired and retry budget exhausted'
                    ELSE 'abandoned: lease expired, re-queued'
                END,
                available_at = CASE
                    WHEN attempts + 1 >= max_attempts THEN available_at
                    ELSE now() + make_interval(secs => :base * (2 ^ attempts))
                END
            WHERE status = 'running'
              AND lease_until < now()
            """
        ),
        {"base": BACKOFF_BASE_SECONDS},
    )
    await session.flush()
    return result.rowcount or 0


async def _owned_running_job(
    session: AsyncSession, job_id: int, owner_token: str
) -> BackgroundJob:
    now = datetime.now(UTC)
    job = (
        await session.scalar(
            select(BackgroundJob)
            .where(
                BackgroundJob.id == job_id,
                BackgroundJob.locked_by == owner_token,
                BackgroundJob.status == JobStatus.running.value,
                BackgroundJob.lease_until >= now,
            )
            .with_for_update()
        )
    )
    if job is None:
        raise ValueError(
            f"Job {job_id} is not running under owner {owner_token!r} with a live lease"
        )
    return job


async def _transition(
    session: AsyncSession,
    job_id: int,
    *,
    owner_token: str,
    updates: dict[str, Any],
) -> BackgroundJob:
    job = await _owned_running_job(session, job_id, owner_token)
    for column, value in updates.items():
        setattr(job, column, value)
    await session.flush()
    return job
