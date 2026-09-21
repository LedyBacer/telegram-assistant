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
