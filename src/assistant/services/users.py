"""User upsert from the Telegram identity (SPEC §4)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from assistant.models.users import User, UserSettings


async def upsert_user(
    session: AsyncSession,
    *,
    user_id: int,
    first_name: str,
    last_name: str | None = None,
    username: str | None = None,
    is_bot: bool = False,
) -> tuple[User, bool]:
    """Insert or update the user and ensure settings exist.

    Returns the user and whether it was created by this call.
    """
    user = (
        await session.execute(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.settings))
        )
    ).scalar_one_or_none()
    created = user is None
    if created:
        user = User(id=user_id)
        session.add(user)
    user.first_name = first_name
    user.last_name = last_name
    user.username = username
    user.is_bot = is_bot
    if user.settings is None:
        user.settings = UserSettings(user_id=user_id)
    await session.flush()
    return user, created
