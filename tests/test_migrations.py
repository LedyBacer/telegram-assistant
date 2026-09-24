"""Migration tests (SPEC §26): apply the full Alembic chain to a fresh database.

Creates a throwaway database, runs ``alembic upgrade head`` against it with a
real PostgreSQL server, asserts the resulting schema (tables, pgvector,
HNSW index, head stamp), then drops the database. The main test database is
never touched.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import asyncpg

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://assistant:assistant@localhost:5432/assistant",
    ),
)
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from assistant.config import get_settings  # noqa: E402
from assistant.db.base import Base  # noqa: E402  (registers all models)

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_DB = "ta_migration_test"

# V4 P2: a migration must run with ONLY a database connection. These are the
# application-credential / deployment variables that the old
# ``get_settings()``-based env.py transitively required; the regression test
# below proves none of them are needed.
ALEMBIC_DB = "ta_alembic_dbonly"
_CREDENTIAL_ENV_VARS = frozenset(
    {
        "TELEGRAM_BOT_TOKEN",
        "PUBLIC_BASE_URL",
        "CHAT_API_KEY",
        "CHAT_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
    }
)

EXPECTED_TABLES = {
    "users",
    "user_settings",
    "calendar_items",
    "reminders",
    "workout_logs",
    "user_files",
    "file_chunks",
    "chat_messages",
    "user_facts",
    "background_jobs",
    "digests",
    "pending_actions",
    "proactive_settings",
    "nudge_deliveries",
}


def _db_url(database: str) -> str:
    parts = urlparse(os.environ["DATABASE_URL"])
    return urlunparse(parts._replace(path=f"/{database}"))


def _pg_url(database: str) -> str:
    return _db_url(database).replace("+asyncpg", "")


def _alembic_config(database: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def test_alembic_upgrade_head_on_fresh_database() -> None:
    base_db = urlparse(os.environ["DATABASE_URL"]).path.lstrip("/")

    async def _prepare() -> None:
        conn = await asyncpg.connect(_pg_url(base_db))
        try:
            await conn.execute(
                f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)'
            )
            await conn.execute(f'CREATE DATABASE "{MIGRATION_DB}"')
        finally:
            await conn.close()

    asyncio.run(_prepare())

    original_url = os.environ["DATABASE_URL"]
    try:
        os.environ["DATABASE_URL"] = _db_url(MIGRATION_DB)
        get_settings.cache_clear()
        cfg = _alembic_config(MIGRATION_DB)
        command.upgrade(cfg, "head")
    finally:
        os.environ["DATABASE_URL"] = original_url
        get_settings.cache_clear()

    # alembic_version is stamped at the expected head.
    head = ScriptDirectory.from_config(_alembic_config(MIGRATION_DB)).get_current_head()
    assert head is not None

    async def _verify_and_drop() -> None:
        conn = await asyncpg.connect(_pg_url(MIGRATION_DB))
        try:
            stamped = await conn.fetchval(
                "SELECT version_num FROM alembic_version"
            )
            assert stamped == head

            tables = {
                row["table_name"]
                for row in await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }
            # Every ORM table was created, and no unexpected ones appeared
            # (alembic_version is Alembic's own bookkeeping table).
            assert tables - {"alembic_version"} == set(Base.metadata.tables)

            vector = await conn.fetchval(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            assert vector is not None

            index = await conn.fetchval(
                "SELECT indexname FROM pg_indexes "
                "WHERE indexname = 'ix_file_chunks_embedding_hnsw'"
            )
            assert index is not None
        finally:
            await conn.close()
        drop = await asyncpg.connect(_pg_url(base_db))
        try:
            await drop.execute(
                f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)'
            )
        finally:
            await drop.close()

    asyncio.run(_verify_and_drop())


def test_migrations_match_orm_metadata() -> None:
    """The table list tested against the migrations must match the ORM models."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_alembic_needs_only_database_url() -> None:
    """V4 P2: ``alembic upgrade head`` succeeds with ONLY ``DATABASE_URL``.

    Runs the real Alembic CLI in a fresh subprocess whose environment has
    every application credential (Telegram token, AI provider keys,
    ``PUBLIC_BASE_URL``) removed. This proves the migration tooling is
    decoupled from the full application ``Settings`` model: a fresh
    PostgreSQL can be migrated without any other configuration.
    """
    base_db = urlparse(os.environ["DATABASE_URL"]).path.lstrip("/")

    async def _prepare() -> None:
        conn = await asyncpg.connect(_pg_url(base_db))
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{ALEMBIC_DB}" WITH (FORCE)')
            await conn.execute(f'CREATE DATABASE "{ALEMBIC_DB}"')
        finally:
            await conn.close()

    asyncio.run(_prepare())
    try:
        # A genuinely clean environment: only DATABASE_URL is present. Every
        # credential the old get_settings()-based env.py required is gone.
        env = {
            k: v for k, v in os.environ.items() if k not in _CREDENTIAL_ENV_VARS
        }
        env["DATABASE_URL"] = _db_url(ALEMBIC_DB)

        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, (
            "alembic upgrade head failed without application credentials "
            f"(it must need only DATABASE_URL):\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )

        async def _verify() -> None:
            conn = await asyncpg.connect(_pg_url(ALEMBIC_DB))
            try:
                stamped = await conn.fetchval("SELECT version_num FROM alembic_version")
                assert stamped is not None
            finally:
                await conn.close()

        asyncio.run(_verify())
    finally:
        async def _drop() -> None:
            conn = await asyncpg.connect(_pg_url(base_db))
            try:
                await conn.execute(f'DROP DATABASE IF EXISTS "{ALEMBIC_DB}" WITH (FORCE)')
            finally:
                await conn.close()

        asyncio.run(_drop())
