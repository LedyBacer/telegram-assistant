"""Bot entrypoint: ``python -m assistant.bot.main`` (SPEC §5)."""

import asyncio

from aiogram import Bot, Dispatcher

from assistant.bot.handlers import router
from assistant.bot.middlewares import DBSessionMiddleware
from assistant.config import get_settings
from assistant.db.engine import dispose_engine
from assistant.logging import setup_logging


def create_bot() -> Bot:
    """Build the bot.

    No ``parse_mode`` is set (P23): every outbound message is **plain text**,
    so user content (filenames, task titles, AI replies) containing ``<``,
    ``>``, ``&`` or HTML-like strings is always shown verbatim and can never
    be misrendered as markup or rejected by the Bot API for bad HTML.
    """
    settings = get_settings()
    return Bot(token=settings.telegram_bot_token)


async def _run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    bot = create_bot()
    dp = Dispatcher()
    dp.message.outer_middleware(DBSessionMiddleware())
    dp.callback_query.outer_middleware(DBSessionMiddleware())
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await dispose_engine()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
