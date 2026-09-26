"""Bot commands: /start, /cancel, /help, /remember, /facts, /language."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.callbacks import DataCallback, DataItemCallback
from assistant.bot.handlers.common import (
    _ensure_user,
    _render_validation_error,
    _user_lang,
)
from assistant.bot.keyboards import (
    action_kb,
    data_actions_kb,
    data_entity_kb,
    data_kb,
    fact_kb,
    language_kb,
    main_menu_kb,
    reply_kb,
)
from assistant.config import get_settings
from assistant.i18n import t
from assistant.models.calendar_items import CalendarItem
from assistant.models.chat_messages import ChatMessage
from assistant.models.facts import FactStatus, UserFact
from assistant.models.files import UserFile
from assistant.models.pending_actions import PendingAction
from assistant.models.reminders import Reminder, ReminderStatus
from assistant.models.workout_logs import WorkoutLog
from assistant.services import actions as actions_service
from assistant.services import data_management
from assistant.services import facts as facts_service
from assistant.services import files as files_service

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
        reply_markup=reply_kb(lang, base),
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
    await message.answer(t(lang, "help.text"))


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


async def _data_counts(session: AsyncSession, uid: int) -> dict[str, int]:
    async def _count(model, column) -> int:
        stmt = select(func.count()).select_from(model).where(column == uid)
        return (await session.execute(stmt)).scalar_one()

    return {
        "items": await _count(CalendarItem, CalendarItem.user_id),
        "reminders": await _count(Reminder, Reminder.user_id),
        "facts": await _count(UserFact, UserFact.user_id),
        "files": await _count(UserFile, UserFile.user_id),
        "workouts": await _count(WorkoutLog, WorkoutLog.user_id),
        "messages": await _count(ChatMessage, ChatMessage.user_id),
        "actions": await _count(PendingAction, PendingAction.user_id),
    }


async def render_data_overview(
    message: Message,
    session: AsyncSession,
    user,
    *,
    edit: bool = False,
) -> None:
    lang = _user_lang(user)
    counts = await _data_counts(session, user.id)
    body = t(lang, "data.summary", **counts)
    if edit:
        await message.edit_text(body, reply_markup=data_kb(lang))
    else:
        await message.answer(body, reply_markup=data_kb(lang))


@router.message(Command("data"))
async def cmd_data(message: Message, session: AsyncSession) -> None:
    user = await _ensure_user(session, message.from_user)
    await render_data_overview(message, session, user)


@router.callback_query(DataCallback.filter())
async def on_data(
    callback: CallbackQuery,
    callback_data: DataCallback,
    session: AsyncSession,
    state: State,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    lang = _user_lang(user)
    action = callback_data.action

    if action == "overview":
        await render_data_overview(callback.message, session, user, edit=True)
        await callback.answer()
        return

    if action == "cancelled":
        await callback.message.edit_text(
            t(lang, "common.cancelled"), reply_markup=data_kb(lang)
        )
        await callback.answer()
        return

    if action == "confirm":
        await callback.message.edit_text(
            t(lang, "data.confirm"), reply_markup=data_kb(lang, confirm=True)
        )
        await callback.answer()
        return

    if action == "execute":
        storage_keys = await data_management.erase_user_data(session, user)
        await session.commit()
        data_management.discard_storage_keys(storage_keys)
        await state.clear()
        await callback.message.edit_text(t(lang, "data.deleted"))
        await callback.answer()
        return

    if action == "facts":
        facts = await facts_service.list_facts(session, user, limit=20)
        body = (
            t(lang, "facts.empty")
            if not facts
            else "\n".join(
                [t(lang, "data.facts_title")]
                + [f"• #{fact.id} [{fact.status}] {fact.value}" for fact in facts]
            )
        )
        rows = [(fact.id, fact.value) for fact in facts]
        await callback.message.edit_text(
            body, reply_markup=data_entity_kb(lang, "fact", rows)
        )
    elif action == "files":
        files = await files_service.list_files(session, user, limit=20)
        body = (
            t(lang, "files.empty")
            if not files
            else "\n".join(
                [t(lang, "data.files_title")]
                + [
                    f"• #{file.id} {file.original_filename} — {file.state}"
                    for file in files
                ]
            )
        )
        rows = [(file.id, file.original_filename) for file in files]
        await callback.message.edit_text(
            body, reply_markup=data_entity_kb(lang, "file", rows)
        )
    elif action == "reminders":
        reminders = list(
            (
                await session.scalars(
                    select(Reminder)
                    .where(
                        Reminder.user_id == user.id,
                        Reminder.status == ReminderStatus.pending.value,
                    )
                    .order_by(Reminder.fire_at, Reminder.id)
                    .limit(20)
                )
            ).all()
        )
        body = (
            t(lang, "data.reminders_empty")
            if not reminders
            else "\n".join(
                [t(lang, "data.reminders_title")]
                + [
                    f"• #{reminder.id} {reminder.fire_at.isoformat()} — "
                    f"{reminder.message}"
                    for reminder in reminders
                ]
            )
        )
        rows = [(reminder.id, reminder.message) for reminder in reminders]
        await callback.message.edit_text(
            body, reply_markup=data_entity_kb(lang, "reminder", rows)
        )
    elif action == "actions":
        actions = await actions_service.list_actions(
            session, user, status="actionable", limit=20
        )
        body = (
            t(lang, "data.actions_empty")
            if not actions
            else "\n".join(
                [t(lang, "data.actions_title")]
                + [f"• #{item.id} {item.summary}" for item in actions]
            )
        )
        rows = [(item.id, item.summary) for item in actions]
        await callback.message.edit_text(
            body, reply_markup=data_actions_kb(lang, rows)
        )
    else:
        await render_data_overview(callback.message, session, user, edit=True)
    await callback.answer()


@router.callback_query(DataItemCallback.filter())
async def on_data_item(
    callback: CallbackQuery,
    callback_data: DataItemCallback,
    session: AsyncSession,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    lang = _user_lang(user)

    if callback_data.kind == "fact":
        kind, payload = "delete_fact", {"fact_id": callback_data.item_id}
    elif callback_data.kind == "file":
        kind, payload = "delete_file", {"file_id": callback_data.item_id}
    elif callback_data.kind == "reminder":
        kind, payload = "cancel_reminder", {"reminder_id": callback_data.item_id}
    else:
        await callback.answer(t(lang, "common.unavailable"), show_alert=True)
        return

    try:
        pending = await actions_service.propose_action(
            session,
            user,
            kind=kind,
            payload=payload,
            summary=t(lang, "data.pending_change"),
        )
    except (ValueError, LookupError):
        await callback.answer(t(lang, "common.unavailable"), show_alert=True)
        return

    await callback.message.edit_text(
        t(lang, "action.propose", summary=pending.summary),
        reply_markup=action_kb(pending.id, lang),
    )
    await callback.answer()
