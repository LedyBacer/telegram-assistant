"""Shared pytest fixtures: real PostgreSQL via async SQLAlchemy."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from assistant.db.base import Base  # noqa: F401  (ensures models are registered)
from assistant.models import jobs  # noqa: F401

DB_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://assistant:assistant@localhost:5432/assistant",
)

# The app code resolves its database via the cached ``get_settings().database_url``.
# Tests must always hit the test database (``TEST_DATABASE_URL``), never the
# ``DATABASE_URL`` exported in the developer's shell (the Docker hostname), or
# app-side ``get_session_factory()`` calls (e.g. ``files._record_failure``) fail
# to connect. Force it here so the suite is order-independent, matching the
# convention in ``test_api.py`` / ``test_minapp_shell.py``. ``get_settings()``
# is only called lazily (in ``get_engine``), after module import, so this
# assignment takes effect before the first settings read.
os.environ["DATABASE_URL"] = DB_URL


@pytest_asyncio.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(DB_URL)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture()
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    # Clean up jobs between tests so each test starts from an empty queue.
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE background_jobs RESTART IDENTITY CASCADE"))


_ALL_TABLES = (
    "pending_actions",
    "digests",
    "file_chunks",
    "user_files",
    "chat_messages",
    "user_facts",
    "workout_logs",
    "reminders",
    "calendar_items",
    "background_jobs",
    "user_settings",
    "proactive_settings",
    "nudge_deliveries",
    "users",
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(session: AsyncSession) -> AsyncIterator[None]:
    """Start every test from an empty database.

    TRUNCATE runs in the fixture session's own transaction (a separate
    connection would deadlock on AccessExclusiveLock while the fixture
    session holds an open transaction).
    """
    await session.execute(
        text(f"TRUNCATE {', '.join(_ALL_TABLES)} RESTART IDENTITY CASCADE")
    )
    await session.commit()
    yield
