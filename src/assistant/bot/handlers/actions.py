"""Assistant Inbox — confirm/cancel a proposed mutation (SPEC §3)."""

from __future__ import annotations

from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions.calendar import ActionStaleError
from assistant.bot.callbacks import ActionCallback
from assistant.bot.handlers.common import _ensure_user, _user_lang
from assistant.bot.keyboards import main_menu_kb
from assistant.i18n import t
from assistant.services import actions as actions_service

router = Router(name="actions")


@router.callback_query(ActionCallback.filter())
async def on_action(
    callback: CallbackQuery,
    callback_data: ActionCallback,
    session: AsyncSession,
) -> None:
    """Confirm/cancel a proposed mutation (SPEC §3).

    Confirmation marks the durable action ``confirmed`` and executes it in
    the same transaction; execution is idempotent and re-validates the
    payload, ownership, and entity state. A stale target expires the action
    instead of running.
    """
    user = await _ensure_user(session, callback.from_user)
    lang = _user_lang(user)
    action_id = callback_data.action_id
    if callback_data.action == "confirm":
        # Confirm + execute under a row lock so a double-tap cannot
        # double-apply the mutation (SPEC §3).
        try:
            action, _result = await actions_service.confirm_and_execute_action(
                session, user, action_id
            )
            text = t(lang, "action.done", summary=action.summary)
        except ActionStaleError:
            text = t(lang, "action.expired")
        except ValueError:
            text = t(lang, "action.unavailable")
    elif callback_data.action == "cancel":
        try:
            await actions_service.reject_action(session, user, action_id)
            text = t(lang, "action.rejected")
        except ValueError:
            text = t(lang, "action.unavailable")
    else:
        text = t(lang, "common.main_menu")
    await callback.message.edit_text(text, reply_markup=main_menu_kb(lang))
    await callback.answer()
