"""Bot commands: /start, /cancel, /help, /remember, /facts, /language."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.callbacks import DataCallback
from assistant.bot.handlers.common import (
    _ensure_user,
    _render_validation_error,
    _user_lang,
)
from assistant.bot.keyboards import data_kb, fact_kb, language_kb, main_menu_kb, reply_kb
from assistant.config import get_settings
from assistant.i18n import t
from assistant.models.calendar_items import CalendarItem
from assistant.models.chat_messages import ChatMessage
from assistant.models.facts import FactStatus, UserFact
from assistant.models.files import FileChunk, UserFile
from assistant.models.reminders import Reminder
from assistant.models.workout_logs import WorkoutLog
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
    # The reply keyboard is a persistent on-screen panel; the inline menu
    # stays the primary navigation. Two messages keep both (V5.4 P6).
    await message.answer(
        t(lang, "start.welcome", name=user.first_name),
        reply_markup=reply_kb(lang),
    )
    await message.answer(
        t(lang, "start.menu"),
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
    await message.answer(
        t(lang, "help.text"),
        reply_markup=main_menu_kb(lang, get_settings().public_base_url),
    )


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


@router.message(Command("data"))
async def cmd_data(message: Message, session: AsyncSession) -> None:
    """Count the user's stored data and offer an explicit two-step delete
    (V5.4 P6). Counts cover every table keyed on the user."""
    user = await _ensure_user(session, message.from_user)
    lang = _user_lang(user)
    uid = user.id

    async def _count(model, column) -> int:
        stmt = select(func.count()).select_from(model).where(column == uid)
        return (await session.execute(stmt)).scalar_one()

    counts = {
        "items": await _count(CalendarItem, CalendarItem.user_id),
        "reminders": await _count(Reminder, Reminder.user_id),
        "facts": await _count(UserFact, UserFact.user_id),
        "files": await _count(UserFile, UserFile.user_id),
        "workouts": await _count(WorkoutLog, WorkoutLog.user_id),
        "messages": await _count(ChatMessage, ChatMessage.user_id),
    }
    await message.answer(
        t(lang, "data.summary", **counts), reply_markup=data_kb(lang)
    )


@router.callback_query(DataCallback.filter())
async def on_data(
    callback: CallbackQuery,
    callback_data: DataCallback,
    session: AsyncSession,
    state: State,
) -> None:
    """Two-step, explicit delete of all the user's data (V5.4 P6)."""
    user = await _ensure_user(session, callback.from_user)
    lang = _user_lang(user)
    if callback_data.action == "confirm":
        await callback.message.edit_text(
            t(lang, "data.confirm"), reply_markup=data_kb(lang, confirm=True)
        )
    elif callback_data.action == "cancelled":
        await callback.message.edit_text(
            t(lang, "common.cancelled"), reply_markup=main_menu_kb(lang)
        )
    else:
        uid = user.id
        # Children before parents; FKs on user_files/chunks cascade, but
        # explicit order keeps the intent readable.
        await session.execute(delete(FileChunk).where(FileChunk.user_id == uid))
        await session.execute(delete(UserFile).where(UserFile.user_id == uid))
        await session.execute(delete(ChatMessage).where(ChatMessage.user_id == uid))
        await session.execute(delete(Reminder).where(Reminder.user_id == uid))
        await session.execute(
            delete(CalendarItem).where(CalendarItem.user_id == uid)
        )
        await session.execute(delete(UserFact).where(UserFact.user_id == uid))
        await session.execute(
            delete(WorkoutLog).where(WorkoutLog.user_id == uid)
        )
        await state.clear()
        await callback.message.edit_text(
            t(lang, "data.deleted"), reply_markup=main_menu_kb(lang)
        )
    await callback.answer()
