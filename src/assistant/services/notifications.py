"""Outbound Telegram delivery for non-bot processes (SPEC §8, §16).

The worker delivers reminders and daily digests through the Bot API, so it
uses its own lazily-created ``Bot`` instance (same token as the bot
process). Tests replace :func:`send_text` with a fake.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from assistant.config import get_settings

logger = logging.getLogger("assistant.notifications")

_bot: Bot | None = None


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        settings = get_settings()
        _bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
    return _bot


async def send_text(chat_id: int, text: str) -> None:
    """Send a plain/HTML text message to a chat. Raises on failure so the
    caller (job handler) can re-queue with backoff."""
    await _get_bot().send_message(chat_id, text)


async def close() -> None:
    """Close the HTTP session (called on worker shutdown)."""
    global _bot
    if _bot is not None:
        await _bot.session.close()
        _bot = None
