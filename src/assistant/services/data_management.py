"""User-data management shared by Telegram surfaces.

A full erase deletes the user row so every ON DELETE CASCADE-owned entity
follows the database schema. Background jobs use ON DELETE SET NULL, so they
are explicitly removed first. Physical file artifacts are removed only after
the database commit succeeds.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.models.files import UserFile
from assistant.models.jobs import BackgroundJob
from assistant.models.users import User
from assistant.services import files as files_service


async def erase_user_data(session: AsyncSession, user: User) -> list[str]:
    """Delete all durable data for ``user`` (flush only)."""
    storage_keys = list(
        (
            await session.scalars(
                select(UserFile.storage_key).where(UserFile.user_id == user.id)
            )
        ).all()
    )
    await session.execute(
        delete(BackgroundJob).where(BackgroundJob.user_id == user.id)
    )
    await session.execute(delete(User).where(User.id == user.id))
    await session.flush()
    return storage_keys


def discard_storage_keys(storage_keys: Iterable[str]) -> None:
    """Idempotent post-commit disk cleanup."""
    for storage_key in storage_keys:
        files_service.discard_storage(storage_key)
