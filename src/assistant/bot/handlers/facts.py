"""Fact (memory) confirm/reject/delete callbacks."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.callbacks import FactCallback
from assistant.bot.handlers.common import _ensure_user, _user_lang
from assistant.bot.keyboards import main_menu_kb
from assistant.i18n import t
from assistant.services import facts as facts_service

router = Router(name="facts")


@router.callback_query(FactCallback.filter())
async def on_fact(
    callback: CallbackQuery,
    callback_data: FactCallback,
    session: AsyncSession,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    lang = _user_lang(user)
    if callback_data.action == "confirm":
        fact = await facts_service.confirm_fact(
            session, user, callback_data.fact_id
        )
        text = (
            t(lang, "facts.stored", value=fact.value)
            if fact is not None
            else t(lang, "facts.unavailable")
        )
    elif callback_data.action == "reject":
        fact = await facts_service.reject_fact(
            session, user, callback_data.fact_id
        )
        text = (
            t(lang, "facts.rejected", value=fact.value)
            if fact is not None
            else t(lang, "facts.unavailable")
        )
    elif callback_data.action == "delete":
        deleted = await facts_service.delete_fact(
            session, user, callback_data.fact_id
        )
        text = (
            t(lang, "facts.deleted")
            if deleted
            else t(lang, "facts.unavailable")
        )
    else:
        text = t(lang, "common.main_menu")
    await callback.message.edit_text(text, reply_markup=main_menu_kb(lang))
    await callback.answer()
