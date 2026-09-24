"""Calendar item complete/cancel callback (SPEC §7)."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.callbacks import ItemCallback
from assistant.bot.handlers.common import _ensure_user, _user_lang, _user_tz
from assistant.bot.keyboards import main_menu_kb
from assistant.i18n import t
from assistant.services import calendar as calendar_service
from assistant.services import reminders as reminders_service

router = Router(name="items")


@router.callback_query(ItemCallback.filter())
async def on_item(
    callback: CallbackQuery,
    callback_data: ItemCallback,
    session: AsyncSession,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    tz = _user_tz(user)
    lang = _user_lang(user)
    if callback_data.action == "complete":
        item = await calendar_service.complete_item(
            session, user, callback_data.item_id
        )
        if item is None:
            text = t(lang, "common.unavailable")
        else:
            # A completed item's pending reminders are no longer useful.
            await reminders_service.cancel_item_reminders(
                session, user, item.id
            )
            when = (
                t(
                    lang,
                    "tasks.was",
                    time=item.starts_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                )
                if item.starts_at
                else ""
            )
            text = t(lang, "tasks.done", title=item.title, when=when)
    elif callback_data.action == "cancel":
        item = await calendar_service.cancel_item(
            session, user, callback_data.item_id
        )
        text = (
            t(lang, "tasks.cancelled", title=item.title)
            if item is not None
            else t(lang, "common.unavailable")
        )
    else:
        text = t(lang, "common.main_menu")
    await callback.message.edit_text(text, reply_markup=main_menu_kb(lang))
    await callback.answer()
