# Deterministic handlers for the persistent reply keyboard.

from __future__ import annotations

from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.state import State
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.handlers.commands import render_data_overview
from assistant.bot.handlers.common import _ensure_user, _fmt_items, _user_lang, _user_tz
from assistant.bot.keyboards import (
    REPLY_SECTIONS,
    items_kb,
    reply_kb,
    reply_section_for_text,
    settings_kb,
)
from assistant.bot.states import TaskDraftStates
from assistant.config import get_settings
from assistant.i18n import t
from assistant.services import calendar as calendar_service

router = Router(name="reply-navigation")

_NAV_LABELS = {
    t(language, key)
    for language in ("ru", "en")
    for _section, key in REPLY_SECTIONS
}


@router.message(F.text.in_(_NAV_LABELS))
async def on_reply_navigation(
    message: Message,
    session: AsyncSession,
    state: State,
) -> None:
    user = await _ensure_user(session, message.from_user)
    lang = _user_lang(user)
    section = reply_section_for_text(lang, message.text or "")
    if section is None:
        return

    base_url = get_settings().public_base_url
    tz = _user_tz(user)

    if section == "task":
        await state.set_state(TaskDraftStates.waiting_for_text)
        await message.answer(
            t(lang, "draft.help"),
            reply_markup=reply_kb(lang, base_url),
        )
    elif section == "today":
        items = await calendar_service.list_today(session, user)
        day = datetime.now(tz=tz).date()
        await message.answer(
            t(lang, "tasks.today_title", date=day.isoformat())
            + "\n"
            + _fmt_items(items, tz, lang),
            reply_markup=items_kb(items, lang)
            if items
            else reply_kb(lang, base_url),
        )
    elif section == "upcoming":
        items = await calendar_service.list_upcoming(session, user)
        await message.answer(
            t(lang, "tasks.upcoming_title")
            + "\n"
            + _fmt_items(items, tz, lang),
            reply_markup=items_kb(items, lang)
            if items
            else reply_kb(lang, base_url),
        )
    elif section == "data":
        await render_data_overview(message, session, user)
    elif section == "settings":
        await message.answer(
            t(lang, "settings.prompt"),
            reply_markup=settings_kb(lang),
        )
    elif section == "miniapp":
        await message.answer(
            t(lang, "menu.miniapp_hint"),
            reply_markup=reply_kb(lang, base_url),
        )
