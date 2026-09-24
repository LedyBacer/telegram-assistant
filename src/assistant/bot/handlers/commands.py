"""Bot commands: /start, /cancel, /help, /remember, /facts, /language."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.handlers.common import (
    _ensure_user,
    _render_validation_error,
    _user_lang,
)
from assistant.bot.keyboards import fact_kb, language_kb, main_menu_kb
from assistant.config import get_settings
from assistant.i18n import t
from assistant.models.facts import FactStatus
from assistant.services import facts as facts_service

router = Router(name="commands")


@router.message(CommandStart())
async def cmd_start(
    message: Message, session: AsyncSession, state: State
) -> None:
    tg_user = message.from_user
    user = await _ensure_user(session, tg_user)
    lang = _user_lang(user)
    await state.clear()
    base = get_settings().public_base_url
    await message.answer(
        t(lang, "start.welcome", name=user.first_name),
        reply_markup=main_menu_kb(lang, base),
    )


@router.message(Command("cancel"))
async def cmd_cancel(
    message: Message, session: AsyncSession, state: State
) -> None:
    user = await _ensure_user(session, message.from_user)
    lang = _user_lang(user)
    await state.clear()
    await message.answer(t(lang, "common.cancelled"), reply_markup=main_menu_kb(lang))


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession) -> None:
    user = await _ensure_user(session, message.from_user)
    lang = _user_lang(user)
    await message.answer(t(lang, "help.text"), reply_markup=main_menu_kb(lang))


@router.message(Command("remember"))
async def cmd_remember(
    message: Message, session: AsyncSession
) -> None:
    user = await _ensure_user(session, message.from_user)
    lang = _user_lang(user)
    text = (message.text or "").partition(" ")[2].strip()
    if not text:
        await message.answer(
            t(lang, "facts.usage"), reply_markup=main_menu_kb(lang)
        )
        return
    try:
        fact = await facts_service.propose_fact(
            session, user, value=text, provenance="telegram:/remember"
        )
    except ValueError as exc:
        await message.answer(_render_validation_error(exc, lang, "facts.cant_store"))
        return
    await message.answer(
        t(lang, "facts.proposed", value=fact.value),
        reply_markup=fact_kb(fact.id, confirmable=True, language=lang),
    )


@router.message(Command("facts"))
async def cmd_facts(message: Message, session: AsyncSession) -> None:
    user = await _ensure_user(session, message.from_user)
    lang = _user_lang(user)
    facts = await facts_service.list_facts(session, user, limit=20)
    if not facts:
        await message.answer(
            t(lang, "facts.empty"), reply_markup=main_menu_kb(lang)
        )
        return
    lines = [t(lang, "facts.list")]
    for fact in facts:
        lines.append(t(lang, "facts.item", status=fact.status, value=fact.value))
    await message.answer(
        "\n".join(lines),
        reply_markup=fact_kb(
            facts[0].id,
            confirmable=facts[0].status == FactStatus.proposed.value,
            language=lang,
        ),
    )


@router.message(Command("language"))
async def cmd_language(
    message: Message, session: AsyncSession, state: State
) -> None:
    """Explicit language change (persisted per user in PostgreSQL)."""
    user = await _ensure_user(session, message.from_user)
    lang = _user_lang(user)
    await state.clear()
    await message.answer(
        t(lang, "settings.language_prompt"),
        reply_markup=language_kb(lang),
    )
