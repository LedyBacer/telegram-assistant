"""Async Alembic migration environment.

Migrations depend only on a database connection. The database URL is read
directly from the ``DATABASE_URL`` environment variable, so running
``alembic upgrade head`` never requires the full application configuration
(Telegram bot token, AI provider keys, ``PUBLIC_BASE_URL``, ...). This
decouples the migration tooling from unrelated application credentials (V4):
a fresh PostgreSQL can be migrated with ``DATABASE_URL`` and nothing else.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Import all models so Base.metadata is fully populated for autogenerate.
from assistant import models  # noqa: F401
from assistant.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    """Resolve the migration database URL directly from the environment.

    Reads ``DATABASE_URL`` in isolation — it does NOT construct the full
    application ``Settings`` model, so migrations never require the Telegram
    bot token, AI provider keys, or ``PUBLIC_BASE_URL``.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Migrations require only a database "
            "connection: set DATABASE_URL to a SQLAlchemy async URL "
            "(e.g. postgresql+asyncpg://user:pass@host:5432/db) and nothing "
            "else."
        )
    return url


config.set_main_option("sqlalchemy.url", _database_url())

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
