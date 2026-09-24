"""Main menu navigation callback (SPEC §5)."""

from __future__ import annotations

from datetime import datetime

from aiogram import Router
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.callbacks import MenuCallback
from assistant.bot.handlers.common import _ensure_user, _fmt_items, _user_lang, _user_tz
from assistant.bot.keyboards import (
    items_kb,
    main_menu_kb,
    settings_kb,
    workouts_kb,
)
from assistant.bot.states import TaskDraftStates, WorkoutStates
from assistant.i18n import t
from assistant.models.files import UserFile
from assistant.services import calendar as calendar_service
from assistant.services import workouts as workouts_service

router = Router(name="menu")


@router.callback_query(MenuCallback.filter())
async def on_menu(
    callback: CallbackQuery,
    callback_data: MenuCallback,
    session: AsyncSession,
    state: State,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    tz = _user_tz(user)
    lang = _user_lang(user)
    section = callback_data.section

    if section == "main":
        await callback.message.edit_text(
            t(lang, "common.main_menu"), reply_markup=main_menu_kb(lang)
        )
    elif section == "task":
        await state.set_state(TaskDraftStates.waiting_for_text)
        await callback.message.edit_text(t(lang, "draft.help"))
    elif section == "today":
        day = datetime.now(tz=tz).date()
        items = await calendar_service.list_today(session, user)
        await callback.message.edit_text(
            t(lang, "tasks.today_title", date=day.isoformat())
            + "\n"
            + _fmt_items(items, tz, lang),
            reply_markup=items_kb(items, lang) if items else main_menu_kb(lang),
        )
    elif section == "upcoming":
        items = await calendar_service.list_upcoming(session, user)
        await callback.message.edit_text(
            t(lang, "tasks.upcoming_title") + "\n" + _fmt_items(items, tz, lang),
            reply_markup=items_kb(items, lang) if items else main_menu_kb(lang),
        )
    elif section == "workouts":
        stats = await workouts_service.workout_stats(session, user)
        logs = await workouts_service.list_workouts(session, user, limit=5)
        lines = [
            t(
                lang,
                "workouts.stats",
                total=stats["total"],
                week=stats["this_week"],
                streak=stats["current_streak"],
                best=stats["longest_streak"],
            )
        ]
        if logs:
            lines.append(t(lang, "workouts.recent"))
            lines.extend(
                f"🏋️ {log.started_at.astimezone(tz):%Y-%m-%d %H:%M}  {log.name}"
                + (
                    t(lang, "workouts.minutes", minutes=log.duration_minutes)
                    if log.duration_minutes
                    else ""
                )
                for log in logs
            )
        else:
            lines.append(t(lang, "workouts.empty"))
        await callback.message.edit_text("\n".join(lines), reply_markup=workouts_kb(lang))
    elif section == "log_workout":
        await state.set_state(WorkoutStates.waiting_log)
        await callback.message.edit_text(t(lang, "workouts.log_prompt"))
    elif section == "schedule_workout":
        await state.set_state(WorkoutStates.waiting_schedule)
        await callback.message.edit_text(t(lang, "workouts.schedule_prompt"))
    elif section == "files":
        files = (
            (
                await session.execute(
                    select(UserFile)
                    .where(UserFile.user_id == user.id)
                    .order_by(UserFile.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        if not files:
            body = t(lang, "files.empty")
        else:
            body = "\n".join(
                t(
                    lang,
                    "files.item",
                    filename=f.original_filename,
                    state=f.state.value,
                    size=f.size_bytes,
                )
                for f in files
            )
        await callback.message.edit_text(body, reply_markup=main_menu_kb(lang))
    elif section == "ask":
        await callback.message.edit_text(
            t(lang, "ask.prompt"), reply_markup=main_menu_kb(lang)
        )
    elif section == "settings":
        await callback.message.edit_text(
            t(lang, "settings.prompt"), reply_markup=settings_kb(lang)
        )
    else:
        await callback.message.edit_text(
            t(lang, "common.main_menu"), reply_markup=main_menu_kb(lang)
        )
    await callback.answer()
