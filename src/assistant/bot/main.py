"""Bot entrypoint: ``python -m assistant.bot.main`` (SPEC §5)."""

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, MenuButtonCommands

from assistant.bot.handlers import private_guard, router
from assistant.bot.middlewares import DBSessionMiddleware, LogContextMiddleware
from assistant.config import get_settings
from assistant.db.engine import dispose_engine
from assistant.i18n import t
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


def bot_commands(language: str) -> list[BotCommand]:
    """The /commands menu, localized (V5.4 P6)."""
    return [
        BotCommand(command="start", description=t(language, "cmd.start.desc")),
        BotCommand(command="help", description=t(language, "cmd.help.desc")),
        BotCommand(
            command="remember", description=t(language, "cmd.remember.desc")
        ),
        BotCommand(command="facts", description=t(language, "cmd.facts.desc")),
        BotCommand(command="data", description=t(language, "cmd.data.desc")),
        BotCommand(
            command="language", description=t(language, "cmd.language.desc")
        ),
        BotCommand(command="cancel", description=t(language, "cmd.cancel.desc")),
    ]


async def setup_bot_commands(bot: Bot) -> None:
    """Publish the localized command menu (RU + EN) and set the default
    "commands" menu button for all private chats (V5.4 P6)."""
    for language in ("ru", "en"):
        await bot.set_my_commands(bot_commands(language), language_code=language)
    await bot.set_chat_menu_button(MenuButtonCommands())


async def _run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    bot = create_bot()
    dp = Dispatcher()
    # Log context is registered first so it is the outermost middleware and the
    # user/chat ids are present for every log in the update.
    dp.message.outer_middleware(LogContextMiddleware())
    dp.message.outer_middleware(DBSessionMiddleware())
    dp.callback_query.outer_middleware(LogContextMiddleware())
    dp.callback_query.outer_middleware(DBSessionMiddleware())
    # The private-chats guard must be checked before any bot handler
    # (V3 P25): non-private updates get a localized explanation and stop.
    dp.include_router(private_guard)
    dp.include_router(router)

    await setup_bot_commands(bot)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await dispose_engine()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
