"""Tests for the durable job queue (SPEC §9) against real PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.services import jobs as js


@pytest.mark.parametrize("use_key", [True, False])
async def test_create_job(session: AsyncSession, use_key: bool) -> None:
    job = await js.create_job(
        session,
        type="echo",
        payload={"x": 1},
        idempotency_key="k-1" if use_key else None,
    )
    await session.commit()
    assert job.id is not None
    assert job.status == JobStatus.pending.value
    assert job.attempts == 0
    assert job.idempotency_key == ("k-1" if use_key else None)


async def test_idempotency_key_returns_existing(session: AsyncSession) -> None:
    first = await js.create_job(session, type="echo", idempotency_key="dup")
    await session.commit()
    second = await js.create_job(session, type="echo", idempotency_key="dup")
    await session.commit()
    assert second.id == first.id


async def test_claim_jobs_no_double_claim(
    session: AsyncSession, engine: AsyncEngine
) -> None:
    for _ in range(5):
        await js.create_job(session, type="echo")
    await session.commit()

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def claim(worker: str) -> list[int]:
        async with factory() as claimer, claimer.begin():
            claimed = await js.claim_jobs(claimer, worker_id=worker, limit=3)
            return [j.id for j in claimed]

    # Concurrent claimers must never receive the same job id.
    results = await asyncio.gather(*(claim(f"w{i}") for i in range(5)))
    claimed = [jid for ids in results for jid in ids]
    assert len(claimed) == len(set(claimed)), f"duplicate claim: {claimed}"
    assert len(claimed) == 5  # all jobs claimed exactly once overall


async def test_complete_job(session: AsyncSession) -> None:
    await js.create_job(session, type="echo")
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    done = await js.complete_job(session, claimed.id, worker_id="w")
    await session.commit()
    assert done.status == JobStatus.completed.value
    assert done.completed_at is not None


async def test_complete_requires_owner(session: AsyncSession) -> None:
    await js.create_job(session, type="echo")
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w1")
    await session.commit()
    assert claimed is not None
    with pytest.raises(ValueError):
        await js.complete_job(session, claimed.id, worker_id="w2")
    await session.rollback()


async def test_fail_job_requeues_with_backoff(session: AsyncSession) -> None:
    await js.create_job(session, type="echo", max_attempts=3)
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    failed = await js.fail_job(session, claimed.id, worker_id="w", error="boom")
    await session.commit()
    assert failed.status == JobStatus.pending.value
    assert failed.attempts == 1
    # Backoff ~30s in the future.
    assert failed.available_at is not None
    from datetime import UTC, datetime

    assert failed.available_at > datetime.now(UTC)


async def test_fail_job_max_attempts_marks_failed(session: AsyncSession) -> None:
    await js.create_job(session, type="echo", max_attempts=1)
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    failed = await js.fail_job(session, claimed.id, worker_id="w", error="boom")
    await session.commit()
    assert failed.status == JobStatus.failed.value
    assert failed.attempts == 1
    assert failed.last_error == "boom"


async def test_cancel_job(session: AsyncSession) -> None:
    job = await js.create_job(session, type="echo")
    await session.commit()
    cancelled = await js.cancel_job(session, job.id)
    await session.commit()
    assert cancelled.status == JobStatus.cancelled.value


async def test_cancel_terminal_returns_unchanged(session: AsyncSession) -> None:
    await js.create_job(session, type="echo")
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    await js.complete_job(session, claimed.id, worker_id="w")
    await session.commit()
    result = await js.cancel_job(session, claimed.id)
    await session.commit()
    assert result.status == JobStatus.completed.value


async def test_recover_abandoned_locks(session: AsyncSession) -> None:
    job = await js.create_job(session, type="echo")
    await session.commit()
    claimed = await js.claim_job(session, worker_id="dead")
    await session.commit()
    assert claimed is not None
    # Fresh lock: nothing recovered.
    assert await js.recover_abandoned_locks(session, ttl_seconds=600) == 0
    await session.commit()
    # Age the lock beyond the TTL.
    from sqlalchemy import text

    async with session.begin():
        await session.execute(text("UPDATE background_jobs SET locked_at = now() - interval '20 minutes'"))
    recovered = await js.recover_abandoned_locks(session, ttl_seconds=600)
    await session.commit()
    assert recovered == 1
    refreshed = await session.scalar(
        select(BackgroundJob)
        .where(BackgroundJob.id == job.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed is not None
    assert refreshed.status == JobStatus.pending.value
    assert refreshed.locked_by is None
