"""Background worker process (SPEC §9).

Polls the durable PostgreSQL job queue with ``FOR UPDATE SKIP LOCKED``
claims, dispatches each job to a registered handler, and records outcomes
via explicit transactions. Safe to run multiple instances concurrently;
abandoned locks (dead workers) are recovered back to pending.

Entrypoint: ``python -m assistant.worker.main``
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import platform
import signal
import uuid

from assistant.config import get_settings
from assistant.db import dispose_engine, get_session_factory
from assistant.logging import log_context, setup_logging
from assistant.models.jobs import BackgroundJob
from assistant.services import digests as digests_service
from assistant.services import files as files_service
from assistant.services import jobs as jobs_service
from assistant.services import notifications
from assistant.services import proactivity as proactivity_service
from assistant.worker import (
    handlers,  # noqa: F401  (registers job handlers)
    registry,
)

logger = logging.getLogger("assistant.worker")


class JobLease:
    """Supervised lease for one claimed job (V4 §4).

    The heartbeat renews the lease on a cadence well inside ``LEASE_SECONDS``
    for the *entire* handler lifetime. If a renewal cannot be made — the lease
    expired and the job was recovered, it was cancelled, *or the renewal
    itself raised* — ``lost`` is set and the worker cancels the handler so its
    in-flight work is abandoned (not orphaned as a half-committed domain write).
    """

    def __init__(self, session_factory, job_id: int, owner_token: str) -> None:
        self._session_factory = session_factory
        self.job_id = job_id
        self.owner_token = owner_token
        self.lost = asyncio.Event()
        self._wakeup = asyncio.Event()

    def stop(self) -> None:
        """Wake the heartbeat so it exits without waiting out the cadence."""
        self._wakeup.set()

    def raise_if_lost(self) -> None:
        if self.lost.is_set():
            raise jobs_service.LeaseLostError(f"lease for job {self.job_id} was lost")

    async def heartbeat(self) -> None:
        while not self.lost.is_set():
            self._wakeup.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wakeup.wait(), timeout=jobs_service.LEASE_SECONDS / 2
                )
            if self._wakeup.is_set():
                break
            try:
                async with self._session_factory() as session:
                    alive = await jobs_service.renew_lease(
                        session, self.job_id, owner_token=self.owner_token
                    )
                    await session.commit()
            except Exception:
                # A renewal that errors means we no longer control the lease:
                # treat it as lost so the handler stops committing.
                logger.exception("lease renewal for job %s failed", self.job_id)
                self.lost.set()
                break
            if not alive:
                logger.warning("job %s lost its lease (recovered/cancelled)", self.job_id)
                self.lost.set()
                break


def _new_worker_id() -> str:
    return f"{platform.node()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


class JobWorker:
    def __init__(self, worker_id: str | None = None) -> None:
        settings = get_settings()
        self.worker_id = worker_id or _new_worker_id()
        self.poll_interval = settings.worker_poll_interval_seconds
        self.batch_size = settings.worker_batch_size
        self.digest_interval = settings.digest_schedule_interval_seconds
        self._session_factory = get_session_factory()
        self._stopping = False
        self._last_digest_pass = 0.0
        self._last_proactive_pass = 0.0

    def request_stop(self) -> None:
        self._stopping = True

    async def poll_once(self) -> int:
        """Claim a batch of jobs and execute them. Returns jobs claimed."""
        async with self._session_factory() as session:
            jobs = await jobs_service.claim_jobs(
                session, worker_id=self.worker_id, limit=self.batch_size
            )
            await session.commit()
        if not jobs:
            return 0
        # Each claimed job carries its owner token in locked_by; pass it
        # through so only this claim may renew/complete/fail the job.
        task_args = [(j.id, j.type, j.locked_by) for j in jobs]
        await asyncio.gather(
            *(
                self._run_job(job_id, job_type, owner_token)
                for job_id, job_type, owner_token in task_args
            )
        )
        return len(task_args)

    async def _run_handler(self, handler, job_id: int, job_type: str, lease: JobLease) -> None:
        """Run one handler for a claimed job.

        Transaction-ownership contract (SPEC §5): each handler owns its domain
        transaction boundaries and commits its own units of work. A handler
        that needs intermediate commits (file ingestion: download → extract →
        embed → chunk-write) is therefore NOT wrapped in an outer worker
        transaction — doing so would let a single long transaction span
        external I/O and make the intermediate ``commit()`` calls ambiguous.
        """
        async with self._session_factory() as session:
            # Short read transaction to load the job row. The handler owns its
            # own transactions from here on.
            async with session.begin():
                job = await session.get(BackgroundJob, job_id)
                if job is None:
                    return
            # Bind job correlation (SPEC §23) so every log the handler emits —
            # including nested AI/embedding/file-ingestion logs — carries the
            # job id, type, and owning user for diagnosis.
            with log_context(job_id=job_id, job_type=job_type, user_id=job.user_id):
                lease.raise_if_lost()
                try:
                    await handler(session, job)
                except Exception:
                    # Release any in-flight handler transaction; the failure
                    # record is written by _fail in a fresh session, so this
                    # cleanup cannot disturb it.
                    if session.in_transaction():
                        await session.rollback()
                    raise

    async def _run_job(self, job_id: int, job_type: str, owner_token: str) -> None:
        """Run one already-claimed job under a supervised lease (V4 §4).

        The heartbeat runs for the *entire* handler lifetime and the handler is
        cancelled the moment the lease is lost (renewal failed, expired, or the
        job was recovered/cancelled), so an old owner can never commit domain
        side-effects under a stale lease. Both the handler and heartbeat tasks
        are always awaited (never just cancelled-and-forgotten).
        """
        handler = registry.handlers.get(job_type)
        if handler is None:
            await self._fail(job_id, owner_token, f"no handler registered for job type {job_type!r}")
            return
        lease = JobLease(self._session_factory, job_id, owner_token)
        # create_task copies the current context, so the handler task sees the
        # lease via jobs_service.current_lease; the token lets us reset it.
        lease_token = jobs_service.current_lease.set(lease)
        heartbeat = asyncio.create_task(lease.heartbeat(), name=f"lease-{job_id}")
        handler_task = asyncio.create_task(
            self._run_handler(handler, job_id, job_type, lease), name=f"job-{job_id}"
        )
        try:
            done, _pending = await asyncio.wait(
                {handler_task, heartbeat}, return_when=asyncio.FIRST_COMPLETED
            )
            if heartbeat in done:
                # The heartbeat exited on its own: the lease was lost. Cancel
                # the handler so its in-flight work is abandoned, fully await
                # the cancellation, and do NOT complete/fail (we no longer
                # own the job — recovery or a new owner does).
                lease.lost.set()
                handler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await handler_task
                return
            # The handler finished first: stop the heartbeat and await it.
            lease.stop()
            await heartbeat
            exc = handler_task.exception()
            if exc is not None:
                logger.exception(
                    "job %s (%s) failed", job_id, job_type, exc_info=exc
                )
                await self._fail(job_id, owner_token, str(exc) or exc.__class__.__name__)
            else:
                await self._complete(job_id, owner_token)
        finally:
            # Belt and braces: neither task may outlive the job.
            for task in (handler_task, heartbeat):
                if not task.done():
                    task.cancel()
            jobs_service.current_lease.reset(lease_token)

    async def _complete(self, job_id: int, owner_token: str) -> None:
        async with self._session_factory() as session:
            try:
                await jobs_service.complete_job(session, job_id, owner_token=owner_token)
                await session.commit()
            except ValueError:
                # Lease already expired / recovered by another worker.
                await session.rollback()
                logger.warning("job %s no longer owned, skipping complete", job_id)

    async def _fail(self, job_id: int, owner_token: str, error: str) -> None:
        async with self._session_factory() as session:
            try:
                await jobs_service.fail_job(session, job_id, owner_token=owner_token, error=error)
                await session.commit()
            except ValueError:
                # Job was recovered by another worker in the meantime.
                await session.rollback()
                logger.warning("job %s no longer owned, skipping fail", job_id)

    async def reap_file_artifacts(self) -> None:
        """One bounded pass reaping disk artifacts of terminally failed file
        ingests whose bytes are re-sourcable (SPEC §21/P22 consistency)."""
        async with self._session_factory() as session:
            reaped = await files_service.reap_terminal_artifacts(session)
            if reaped:
                logger.info("reaped %d terminal file artifact(s)", reaped)

    async def schedule_digests(self) -> None:
        """One idempotent pass ensuring every user has today's digest queued."""
        async with self._session_factory() as session:
            created = await digests_service.ensure_digest_jobs(session)
            if created:
                logger.info("scheduled %d new digest(s)", created)
            await session.commit()

    async def proactive_pass(self) -> None:
        """One bounded proactivity pass plus pending-action expiry.

        Idempotent via NudgeDelivery dedupe rows; a per-user failure rolls
        back only that user and retries on the next pass.
        """
        async with self._session_factory() as session:
            result = await proactivity_service.run_proactive_pass(session)
            if result["nudges_sent"] or result["actions_expired"]:
                logger.info(
                    "proactive pass: %d nudge(s), %d expired action(s)",
                    result["nudges_sent"],
                    result["actions_expired"],
                )

    async def run(self) -> None:
        logger.info("worker %s starting (poll=%.2fs batch=%d)", self.worker_id, self.poll_interval, self.batch_size)
        loop = asyncio.get_running_loop()
        self._last_digest_pass = loop.time()
        try:
            while not self._stopping:
                started = loop.time()
                try:
                    async with self._session_factory() as session:
                        recovered = await jobs_service.recover_abandoned(session)
                        if recovered:
                            logger.info("recovered %d abandoned job(s)", recovered)
                        await session.commit()
                    if loop.time() - self._last_digest_pass >= self.digest_interval:
                        self._last_digest_pass = loop.time()
                        await self.schedule_digests()
                        try:
                            await self.reap_file_artifacts()
                        except Exception:
                            logger.exception("file artifact reap failed")
                    if loop.time() - self._last_proactive_pass >= self.digest_interval:
                        self._last_proactive_pass = loop.time()
                        try:
                            await self.proactive_pass()
                        except Exception:
                            logger.exception("proactive pass failed")
                    await self.poll_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("worker poll iteration failed")
                    await asyncio.sleep(self.poll_interval)
                    continue
                elapsed = loop.time() - started
                await asyncio.sleep(max(0.0, self.poll_interval - elapsed))
        finally:
            logger.info("worker %s stopped", self.worker_id)
            await notifications.close()
            await dispose_engine()


async def _run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    worker = JobWorker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)
    await worker.run()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
