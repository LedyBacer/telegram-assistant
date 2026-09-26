"""Settings + language change callbacks (SPEC §20)."""

from __future__ import annotations

from aiogram import Router
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.callbacks import LanguageCallback, SettingsCallback
from assistant.bot.handlers.common import _ensure_user, _user_lang
from assistant.bot.keyboards import language_kb, reply_kb, settings_kb
from assistant.bot.states import SettingsStates
from assistant.config import get_settings
from assistant.i18n import is_supported, t

router = Router(name="settings")


@router.callback_query(SettingsCallback.filter())
async def on_settings(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    session: AsyncSession,
    state: State,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    lang = _user_lang(user)
    if callback_data.action == "timezone":
        await state.set_state(SettingsStates.timezone)
        await callback.message.edit_text(t(lang, "settings.tz_prompt"))
    elif callback_data.action == "digest_time":
        await state.set_state(SettingsStates.digest_time)
        await callback.message.edit_text(t(lang, "settings.digest_prompt"))
    elif callback_data.action == "language":
        await callback.message.edit_text(
            t(lang, "settings.language_prompt"),
            reply_markup=language_kb(lang),
        )
    else:
        await callback.message.edit_text(
            t(lang, "settings.prompt"), reply_markup=settings_kb(lang)
        )
    await callback.answer()


@router.callback_query(LanguageCallback.filter())
async def on_language(
    callback: CallbackQuery,
    callback_data: LanguageCallback,
    session: AsyncSession,
    state: State,
) -> None:
    """Persist the chosen language and re-render immediately in it."""
    user = await _ensure_user(session, callback.from_user)
    code = callback_data.code
    if not is_supported(code):
        lang = _user_lang(user)
        await callback.message.edit_text(
            t(lang, "errors.generic"), reply_markup=settings_kb(lang)
        )
        await callback.answer()
        return
    user.settings.language = code
    await state.clear()
    await callback.message.edit_text(
        t(code, "settings.language_changed", label=t(code, f"settings.language_{code}")),
        reply_markup=settings_kb(code),
    )
    # Reply keyboards can only be attached to a fresh message: re-send the
    # persistent panel in the new language (V5.4 P6).
    await callback.message.answer(
        t(code, "settings.language_changed", label=t(code, f"settings.language_{code}")),
        reply_markup=reply_kb(code, get_settings().public_base_url),
    )
    await callback.answer()
