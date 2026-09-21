"""Background job handler registry (SPEC §9).

A leaf module so services can register handlers without importing the worker
runtime (which would create a circular import).
"""

from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.jobs import BackgroundJob

JobHandler = Callable[[AsyncSession, BackgroundJob], Awaitable[None]]

handlers: dict[str, JobHandler] = {}


def register_job_handler(job_type: str) -> Callable[[JobHandler], JobHandler]:
    """Decorator registering an async handler for a background job type."""

    def decorator(func: JobHandler) -> JobHandler:
        handlers[job_type] = func
        return func

    return decorator
