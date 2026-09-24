"""Per-event database session and log-context middlewares for the bot."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from assistant.db import get_session_factory
from assistant.logging import log_context


class LogContextMiddleware(BaseMiddleware):
    """Bind per-update log context (user/chat id) for the handler (SPEC §23).

    Registered *before* :class:`DBSessionMiddleware` so it is the outermost
    middleware: the user and chat identifiers are present for every log in the
    update, including session-open failures and guard rejections.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        chat = getattr(event, "chat", None)
        with log_context(
            user_id=getattr(user, "id", None) if user else None,
            chat_id=getattr(chat, "id", None) if chat else None,
        ):
            return await handler(event, data)


class DBSessionMiddleware(BaseMiddleware):
    """Open one AsyncSession per incoming update and commit on success."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        factory = get_session_factory()
        async with factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result
