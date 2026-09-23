"""Durable job queue: leases, bounded recovery, caller-safe idempotency.

Covers SPEC §5.2 (real leases: owner token + lease_until + heartbeat +
owner-gated transitions), §5.3 (crash recovery consumes the bounded retry
budget), and §5.4 (idempotent create must not roll back the caller's
transaction).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
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


async def test_idempotent_create_preserves_caller_tx(session: AsyncSession) -> None:
    """§5.4 regression: a duplicate idempotency create must not roll back the
    caller's outer transaction and must return the same job."""
    marker = await js.create_job(session, type="echo")  # caller's prior work
    first = await js.create_job(session, type="echo", idempotency_key="K")
    second = await js.create_job(session, type="echo", idempotency_key="K")
    # The transaction is fully intact: all three rows are visible.
    assert (await session.get(BackgroundJob, marker.id)) is not None
    assert second.id == first.id
    count = await session.scalar(text("SELECT count(*) FROM background_jobs"))
    assert count == 2
    await session.rollback()


async def test_on_conflict_do_nothing_does_not_abort_caller_tx(
    session: AsyncSession,
) -> None:
    """§5.4 mechanism: the PG ON CONFLICT DO NOTHING primitive create_job
    relies on is a no-op on a uniqueness race and never aborts the caller's
    transaction (unlike a SAVEPOINT + flush, which asyncpg aborts wholesale)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    ins = pg_insert(BackgroundJob).values(type="echo", idempotency_key="K")
    await session.execute(ins)
    await session.execute(pg_insert(BackgroundJob).values(type="marker"))
    # A conflicting insert in the SAME caller transaction is a no-op...
    result = await session.execute(
        ins.on_conflict_do_nothing(index_elements=["idempotency_key"])
    )
    assert (result.rowcount or 0) == 0
    # ...and the caller's transaction is NOT aborted.
    count = await session.scalar(text("SELECT count(*) FROM background_jobs"))
    assert count == 2
    await session.rollback()


async def test_claim_sets_lease(session: AsyncSession) -> None:
    await js.create_job(session, type="echo")
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    assert claimed.status == JobStatus.running.value
    assert claimed.locked_by  # owner token present
    assert claimed.lease_until is not None
    assert claimed.lease_until > datetime.now(UTC)


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


async def test_renew_lease_only_for_owner(session: AsyncSession) -> None:
    job = await js.create_job(session, type="echo")
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    token = claimed.locked_by
    before = claimed.lease_until
    # The current owner can renew.
    assert await js.renew_lease(session, job.id, owner_token=token) is True
    await session.commit()
    # A different owner cannot renew the lease.
    assert (
        await js.renew_lease(session, job.id, owner_token=js.new_owner_token("other"))
        is False
    )
    await session.commit()
    refreshed = await session.scalar(
        select(BackgroundJob)
        .where(BackgroundJob.id == job.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed is not None
    assert refreshed.lease_until >= before


async def test_complete_job(session: AsyncSession) -> None:
    await js.create_job(session, type="echo")
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    done = await js.complete_job(session, claimed.id, owner_token=claimed.locked_by)
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
        await js.complete_job(session, claimed.id, owner_token=js.new_owner_token("w2"))
    await session.rollback()


async def test_complete_rejected_after_lease_expiry(session: AsyncSession) -> None:
    """Only a live lease may be completed; an expired lease belongs to nobody."""
    job = await js.create_job(session, type="echo")
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    async with session.begin():
        await session.execute(
            text("UPDATE background_jobs SET lease_until = now() - interval '1 minute'")
        )
    with pytest.raises(ValueError):
        await js.complete_job(session, job.id, owner_token=claimed.locked_by)
    await session.rollback()


async def test_fail_job_requeues_with_backoff(session: AsyncSession) -> None:
    await js.create_job(session, type="echo", max_attempts=3)
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    failed = await js.fail_job(
        session, claimed.id, owner_token=claimed.locked_by, error="boom"
    )
    await session.commit()
    assert failed.status == JobStatus.pending.value
    assert failed.attempts == 1
    assert failed.available_at is not None
    assert failed.available_at > datetime.now(UTC)


async def test_fail_job_max_attempts_marks_failed(session: AsyncSession) -> None:
    await js.create_job(session, type="echo", max_attempts=1)
    await session.commit()
    claimed = await js.claim_job(session, worker_id="w")
    await session.commit()
    assert claimed is not None
    failed = await js.fail_job(
        session, claimed.id, owner_token=claimed.locked_by, error="boom"
    )
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
    await js.complete_job(session, claimed.id, owner_token=claimed.locked_by)
    await session.commit()
    result = await js.cancel_job(session, claimed.id)
    await session.commit()
    assert result.status == JobStatus.completed.value


async def _age_lease(session: AsyncSession) -> None:
    async with session.begin():
        await session.execute(
            text("UPDATE background_jobs SET lease_until = now() - interval '1 minute'")
        )


async def _make_available(session: AsyncSession) -> None:
    """Simulate the backoff elapsing so a re-queued job is claimable again."""
    async with session.begin():
        await session.execute(
            text("UPDATE background_jobs SET available_at = now() WHERE status = 'pending'")
        )


async def test_recover_abandoned_fresh_lease_not_recovered(session: AsyncSession) -> None:
    await js.create_job(session, type="echo")
    await session.commit()
    claimed = await js.claim_job(session, worker_id="dead")
    await session.commit()
    assert claimed is not None
    # Live lease: nothing is recovered.
    assert await js.recover_abandoned(session) == 0
    await session.commit()


async def test_recover_abandoned_requeues_with_backoff(session: AsyncSession) -> None:
    job = await js.create_job(session, type="echo", max_attempts=3)
    await session.commit()
    claimed = await js.claim_job(session, worker_id="dead")
    await session.commit()
    assert claimed is not None
    await _age_lease(session)
    recovered = await js.recover_abandoned(session)
    await session.commit()
    assert recovered == 1
    refreshed = await session.scalar(
        select(BackgroundJob)
        .where(BackgroundJob.id == job.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed is not None
    # Bounded recovery consumes a retry attempt and re-queues with backoff.
    assert refreshed.status == JobStatus.pending.value
    assert refreshed.attempts == 1
    assert refreshed.locked_by is None
    assert refreshed.lease_until is None
    assert refreshed.available_at > datetime.now(UTC)


async def test_recover_abandoned_exhausts_budget(session: AsyncSession) -> None:
    """§5.3: a job that keeps crashing eventually fails, never loops forever."""
    job = await js.create_job(session, type="echo", max_attempts=2)
    await session.commit()
    # First crash: re-queue (attempt 1).
    await js.claim_job(session, worker_id="dead1")
    await session.commit()
    await _age_lease(session)
    assert await js.recover_abandoned(session) == 1
    await session.commit()
    # Let the backoff elapse, then re-claim, crash again:
    # attempt 2 == max_attempts -> failed.
    await _make_available(session)
    await js.claim_job(session, worker_id="dead2")
    await session.commit()
    await _age_lease(session)
    assert await js.recover_abandoned(session) == 1
    await session.commit()
    refreshed = await session.scalar(
        select(BackgroundJob)
        .where(BackgroundJob.id == job.id)
        .execution_options(populate_existing=True)
    )
    assert refreshed is not None
    assert refreshed.status == JobStatus.failed.value
    assert refreshed.attempts == 2
