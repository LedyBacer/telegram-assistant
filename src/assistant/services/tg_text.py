"""Central Telegram text-delivery helpers (V3 P24).

Telegram rejects messages longer than 4096 characters. Instead of every
send site reinventing truncation (or silently failing when the model
produces a long answer), all potentially large output goes through
:func:`split_for_telegram` plus one of the send helpers below.

Splitting prefers paragraph boundaries, then single newlines, and only
falls back to a hard cut when no boundary fits. Segments concatenate
back to the original text with at most one boundary newline dropped per
segment, so no content is ever lost.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from aiogram.types import InlineKeyboardMarkup, Message

#: Hard Telegram Bot API limit for a single message (plain text).
TELEGRAM_TEXT_LIMIT = 4096


def split_for_telegram(
    text: str, limit: int = TELEGRAM_TEXT_LIMIT
) -> list[str]:
    """Split ``text`` into segments of at most ``limit`` characters.

    Boundary preference: ``\\n\\n`` (paragraph), then ``\\n`` (line), then a
    hard cut at ``limit``. Each segment is <= ``limit`` characters; joining
    the segments reproduces the original text (boundary newlines are kept
    at the end of the preceding segment).
    """
    if limit < 1:
        raise ValueError("limit must be positive")
    if len(text) <= limit:
        return [text] if text else []

    segments: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        # Prefer a paragraph break, then a line break, then a hard cut.
        # Minimum segment size avoids degenerate 1-char splits.
        min_cut = limit // 3
        para = window.rfind("\n\n")
        if para >= min_cut:
            cut = para + 2
        else:
            line = window.rfind("\n")
            cut = line + 1 if line >= min_cut else limit
        segments.append(window[:cut])
        rest = rest[cut:]
    segments.append(rest)
    return segments


async def answer_long(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    *,
    limit: int = TELEGRAM_TEXT_LIMIT,
    **kwargs: Any,
) -> Sequence[Message]:
    """Send ``text`` as one or more messages, each within the limit.

    A keyboard (when given) is attached to the *last* segment so it stays
    with the end of the content. Returns the sent messages.
    """
    segments = split_for_telegram(text, limit)
    sent: list[Message] = []
    for i, seg in enumerate(segments):
        if i == len(segments) - 1 and reply_markup is not None:
            sent.append(await message.answer(seg, reply_markup=reply_markup, **kwargs))
        else:
            sent.append(await message.answer(seg, **kwargs))
    return sent


async def send_long(
    bot: Any,
    chat_id: int,
    text: str,
    *,
    limit: int = TELEGRAM_TEXT_LIMIT,
    **kwargs: Any,
) -> None:
    """Send ``text`` to ``chat_id`` via ``bot``, splitting if needed.

    Raises on the first failed segment so the caller (job handler) can
    re-queue; earlier segments, if any, have already been delivered.
    """
    for seg in split_for_telegram(text, limit):
        await bot.send_message(chat_id, seg, **kwargs)
