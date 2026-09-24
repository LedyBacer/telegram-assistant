"""Task draft confirm/cancel callback (SPEC §6)."""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AITaskDraft
from assistant.bot.callbacks import DraftCallback
from assistant.bot.handlers.common import (
    _ai_draft_to_task_draft,
    _ensure_user,
    _parse_draft,
    _user_lang,
    _user_tz,
)
from assistant.bot.keyboards import main_menu_kb
from assistant.i18n import t
from assistant.services import calendar as calendar_service
from assistant.services import reminders as reminders_service

router = Router(name="draft")


@router.callback_query(DraftCallback.filter())
async def on_draft(
    callback: CallbackQuery,
    callback_data: DraftCallback,
    session: AsyncSession,
    state: State,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    lang = _user_lang(user)
    if callback_data.action == "cancel":
        await state.clear()
        await callback.message.edit_text(
            t(lang, "draft.cancelled"), reply_markup=main_menu_kb(lang)
        )
    else:
        data = await state.get_data()
        tz = _user_tz(user)
        # AI-produced drafts are stored typed in FSM state; manual drafts
        # are re-parsed from the raw text.
        if ai_dump := data.get("draft_ai"):
            draft = _ai_draft_to_task_draft(AITaskDraft.model_validate(ai_dump), tz)
        else:
            draft = _parse_draft(data.get("draft_text", ""), tz)
        item = await calendar_service.create_item(
            session,
            user,
            title=draft.title,
            kind=draft.kind,
            description=draft.description,
            starts_at=draft.starts_at,
            ends_at=draft.ends_at,
            due_at=draft.due_at,
            priority=draft.priority,
        )
        reminders = await reminders_service.create_item_reminders(
            session, user, item, offsets_minutes=draft.remind_offsets
        )
        await state.clear()
        text = t(lang, "draft.saved", title=draft.title)
        if reminders:
            text += "\n" + t(lang, "draft.reminders", count=len(reminders))
        await callback.message.edit_text(text, reply_markup=main_menu_kb(lang))
    await callback.answer()
