"""Background worker process (SPEC §9).

Polls the durable PostgreSQL job queue with ``FOR UPDATE SKIP LOCKED``
claims, dispatches each job to a registered handler, and records outcomes
via explicit transactions. Safe to run multiple instances concurrently;
abandoned locks (dead workers) are recovered back to pending.

Entrypoint: ``python -m assistant.worker.main``
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import uuid

from assistant.config import get_settings
from assistant.db import dispose_engine, get_session_factory
from assistant.logging import setup_logging
from assistant.models.jobs import BackgroundJob
from assistant.services import digests as digests_service
from assistant.services import jobs as jobs_service
from assistant.services import notifications
from assistant.worker import (
    handlers,  # noqa: F401  (registers job handlers)
    registry,
)

logger = logging.getLogger("assistant.worker")

# A running job whose lock is older than this is treated as abandoned.
LOCK_TTL_SECONDS = 600.0


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
        task_args = [(j.id, j.type) for j in jobs]
        await asyncio.gather(*(self._run_job(job_id, job_type) for job_id, job_type in task_args))
        return len(task_args)

    async def _run_job(self, job_id: int, job_type: str) -> None:
        handler = registry.handlers.get(job_type)
        if handler is None:
            await self._fail(job_id, f"no handler registered for job type {job_type!r}")
            return
        try:
            async with self._session_factory() as session, session.begin():
                job = await session.get(BackgroundJob, job_id)
                if job is None:
                    return
                await handler(session, job)
        except asyncio.CancelledError:
            # Leave the job running; abandoned-lock recovery re-queues it.
            raise
        except Exception as exc:
            logger.exception("job %s (%s) failed", job_id, job_type)
            await self._fail(job_id, str(exc) or exc.__class__.__name__)
        else:
            await self._complete(job_id)

    async def _complete(self, job_id: int) -> None:
        async with self._session_factory() as session:
            await jobs_service.complete_job(session, job_id, worker_id=self.worker_id)
            await session.commit()

    async def _fail(self, job_id: int, error: str) -> None:
        async with self._session_factory() as session:
            try:
                await jobs_service.fail_job(session, job_id, worker_id=self.worker_id, error=error)
                await session.commit()
            except ValueError:
                # Job was recovered by another worker in the meantime.
                await session.rollback()
                logger.warning("job %s no longer owned by %s, skipping fail", job_id, self.worker_id)

    async def schedule_digests(self) -> None:
        """One idempotent pass ensuring every user has today's digest queued."""
        async with self._session_factory() as session:
            created = await digests_service.ensure_digest_jobs(session)
            if created:
                logger.info("scheduled %d new digest(s)", created)
            await session.commit()

    async def run(self) -> None:
        logger.info("worker %s starting (poll=%.2fs batch=%d)", self.worker_id, self.poll_interval, self.batch_size)
        loop = asyncio.get_running_loop()
        self._last_digest_pass = loop.time()
        try:
            while not self._stopping:
                started = loop.time()
                try:
                    async with self._session_factory() as session:
                        recovered = await jobs_service.recover_abandoned_locks(
                            session, ttl_seconds=LOCK_TTL_SECONDS
                        )
                        if recovered:
                            logger.info("recovered %d abandoned job(s)", recovered)
                        await session.commit()
                    if loop.time() - self._last_digest_pass >= self.digest_interval:
                        self._last_digest_pass = loop.time()
                        await self.schedule_digests()
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
