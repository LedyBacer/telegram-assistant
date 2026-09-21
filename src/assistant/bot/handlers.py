"""Bot handlers: main menu, task drafts, settings (SPEC §5-§7)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot.callbacks import DraftCallback, MenuCallback, SettingsCallback
from assistant.bot.keyboards import draft_kb, main_menu_kb, settings_kb
from assistant.bot.states import SettingsStates, TaskDraftStates
from assistant.config import get_settings
from assistant.models.calendar_items import (
    CalendarItem,
    ItemKind,
    ItemPriority,
    ItemStatus,
)
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.models.files import UserFile
from assistant.models.users import User
from assistant.models.workout_logs import WorkoutLog
from assistant.services.users import upsert_user

router = Router(name="bot")

DRAFT_HELP = (
    "Send the task as lines, e.g.:\n"
    "title: Buy groceries\n"
    "date: 2026-09-21\n"
    "time: 18:30\n"
    "due: 2026-09-21 19:00\n"
    "priority: high\n"
    "kind: event\n"
    "description: milk, bread\n"
    "Only `title` is required. /cancel aborts."
)


@dataclass(slots=True)
class TaskDraft:
    """A parsed, not-yet-persisted calendar item (SPEC §6)."""

    title: str
    kind: ItemKind
    starts_at: datetime | None
    due_at: datetime | None
    priority: ItemPriority
    description: str | None


def _user_tz(user: User) -> ZoneInfo:
    name = user.settings.timezone if user.settings is not None else "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _parse_hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    hour_i, minute_i = int(hour), int(minute or 0)
    if not 0 <= hour_i <= 23 or not 0 <= minute_i <= 59:
        raise ValueError("time must be HH:MM")
    return time(hour_i, minute_i)


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_draft(text: str, tz: ZoneInfo) -> TaskDraft:
    """Parse the manual structured format into a TaskDraft (validated)."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep or not key.strip() or not value.strip():
            raise ValueError(f"Unrecognized line: {line!r}")
        fields[key.strip().lower()] = value.strip()

    title = fields.get("title", "").strip()
    if not title:
        raise ValueError("The `title:` line is required.")
    if len(title) > 500:
        raise ValueError("Title is too long (max 500 characters).")

    kind = ItemKind.task
    if raw := fields.get("kind"):
        try:
            kind = ItemKind(raw)
        except ValueError:
            raise ValueError("kind must be `task` or `event`.") from None

    priority = ItemPriority.normal
    if raw := fields.get("priority"):
        try:
            priority = ItemPriority(raw)
        except ValueError:
            raise ValueError("priority must be `low`, `normal`, or `high`.") from None

    starts_at: datetime | None = None
    day = _parse_date(fields["date"]) if fields.get("date") else None
    clock = _parse_hhmm(fields["time"]) if fields.get("time") else None
    if day is not None or clock is not None:
        starts_at = datetime.combine(
            day or datetime.now(tz).date(), clock or time(9, 0), tzinfo=tz
        ).astimezone(UTC)

    due_at: datetime | None = None
    if raw_due := fields.get("due"):
        due_at = datetime.fromisoformat(raw_due)
        due_at = due_at.replace(tzinfo=tz).astimezone(UTC)

    if len(fields.get("description", "")) > 4000:
        raise ValueError("Description is too long (max 4000 characters).")

    return TaskDraft(
        title=title,
        kind=kind,
        starts_at=starts_at,
        due_at=due_at,
        priority=priority,
        description=fields.get("description") or None,
    )


def _draft_preview(draft: TaskDraft, tz: ZoneInfo) -> str:
    icon = "📆" if draft.kind is ItemKind.event else "✅"
    lines = [f"{icon} {draft.title}", f"kind: {draft.kind.value}", f"priority: {draft.priority.value}"]
    if draft.starts_at:
        lines.append(f"start: {draft.starts_at.astimezone(tz):%Y-%m-%d %H:%M} ({tz})")
    if draft.due_at:
        lines.append(f"due: {draft.due_at.astimezone(tz):%Y-%m-%d %H:%M} ({tz})")
    if draft.description:
        lines.append(f"notes: {draft.description}")
    return "\n".join(lines)


def _fmt_items(items: list[CalendarItem], tz: ZoneInfo) -> str:
    if not items:
        return "Nothing here."
    lines = []
    for item in items:
        icon = {"task": "✅", "event": "📆"}[item.kind]
        when = ""
        if item.starts_at:
            when = item.starts_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        elif item.due_at:
            when = f"due {item.due_at.astimezone(tz):%Y-%m-%d %H:%M}"
        prefix = f"{when}  " if when else ""
        lines.append(f"{icon} {prefix}{item.title} [{item.priority}]")
    return "\n".join(lines)


async def _ensure_user(session: AsyncSession, tg_user: Any) -> User:
    user, _ = await upsert_user(
        session,
        user_id=tg_user.id,
        first_name=tg_user.first_name or "",
        last_name=getattr(tg_user, "last_name", None),
        username=getattr(tg_user, "username", None),
        is_bot=bool(getattr(tg_user, "is_bot", False)),
    )
    return user


@router.message(CommandStart())
async def cmd_start(
    message: Message, session: AsyncSession, state: State
) -> None:
    tg_user = message.from_user
    user = await _ensure_user(session, tg_user)
    await state.clear()
    base = get_settings().public_base_url
    await message.answer(
        f"Hi {user.first_name}! I keep your tasks, events, workouts, and files.\n"
        "Pick a section below.",
        reply_markup=main_menu_kb(base),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: State) -> None:
    await state.clear()
    await message.answer("Cancelled.", reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "Use the menu below. ➕ creates tasks/events, ⚙️ adjusts settings, "
        "/cancel aborts an in-progress step.",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(MenuCallback.filter())
async def on_menu(
    callback: CallbackQuery,
    callback_data: MenuCallback,
    session: AsyncSession,
    state: State,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    tz = _user_tz(user)
    section = callback_data.section
    now = datetime.now(UTC)

    if section == "main":
        await callback.message.edit_text(
            "Main menu:", reply_markup=main_menu_kb()
        )
    elif section == "task":
        await state.set_state(TaskDraftStates.waiting_for_text)
        await callback.message.edit_text(DRAFT_HELP)
    elif section == "today":
        day = datetime.now(tz=tz).date()
        start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
        end = start + timedelta(days=1)
        items = (
            (
                await session.execute(
                    select(CalendarItem)
                    .where(
                        CalendarItem.user_id == user.id,
                        CalendarItem.status == ItemStatus.scheduled.value,
                        CalendarItem.starts_at >= start,
                        CalendarItem.starts_at < end,
                    )
                    .order_by(CalendarItem.starts_at)
                )
            )
            .scalars()
            .all()
        )
        await callback.message.edit_text(
            f"📅 Today ({day.isoformat()}):\n{_fmt_items(list(items), tz)}",
            reply_markup=main_menu_kb(),
        )
    elif section == "upcoming":
        end = now + timedelta(days=7)
        items = (
            (
                await session.execute(
                    select(CalendarItem)
                    .where(
                        CalendarItem.user_id == user.id,
                        CalendarItem.status == ItemStatus.scheduled.value,
                        and_(CalendarItem.starts_at >= now, CalendarItem.starts_at < end),
                    )
                    .order_by(CalendarItem.starts_at)
                )
            )
            .scalars()
            .all()
        )
        await callback.message.edit_text(
            f"📆 Next 7 days:\n{_fmt_items(list(items), tz)}",
            reply_markup=main_menu_kb(),
        )
    elif section == "workouts":
        logs = (
            (
                await session.execute(
                    select(WorkoutLog)
                    .where(WorkoutLog.user_id == user.id)
                    .order_by(WorkoutLog.started_at.desc())
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        if not logs:
            body = "No workouts logged yet."
        else:
            body = "\n".join(
                f"🏋️ {log.started_at.astimezone(tz):%Y-%m-%d %H:%M}  "
                f"{log.name} [{log.status.value}]"
                + (f" ({log.duration_minutes} min)" if log.duration_minutes else "")
                for log in logs
            )
        await callback.message.edit_text(body, reply_markup=main_menu_kb())
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
            body = "No files yet. Send me a document to store it."
        else:
            body = "\n".join(
                f"📄 {f.original_filename} — {f.state.value} ({f.size_bytes} bytes)"
                for f in files
            )
        await callback.message.edit_text(body, reply_markup=main_menu_kb())
    elif section == "ask":
        await callback.message.edit_text(
            "Send me a question as a normal message and I will answer it.",
            reply_markup=main_menu_kb(),
        )
    elif section == "settings":
        await callback.message.edit_text("Settings:", reply_markup=settings_kb())
    else:
        await callback.message.edit_text("Main menu:", reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(SettingsCallback.filter())
async def on_settings(
    callback: CallbackQuery, callback_data: SettingsCallback, state: State
) -> None:
    if callback_data.action == "timezone":
        await state.set_state(SettingsStates.timezone)
        await callback.message.edit_text(
            "Send the IANA timezone, e.g. Europe/Berlin. /cancel aborts."
        )
    elif callback_data.action == "digest_time":
        await state.set_state(SettingsStates.digest_time)
        await callback.message.edit_text(
            "Send the digest time as HH:MM (24-hour). /cancel aborts."
        )
    else:
        await callback.message.edit_text("Settings:", reply_markup=settings_kb())
    await callback.answer()


@router.callback_query(DraftCallback.filter())
async def on_draft(
    callback: CallbackQuery,
    callback_data: DraftCallback,
    session: AsyncSession,
    state: State,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    if callback_data.action == "cancel":
        await state.clear()
        await callback.message.edit_text(
            "Draft cancelled.", reply_markup=main_menu_kb()
        )
    else:
        raw = (await state.get_data()).get("draft_text", "")
        draft = _parse_draft(raw, _user_tz(user))
        item = CalendarItem(
            user_id=user.id,
            kind=draft.kind.value,
            title=draft.title,
            description=draft.description,
            starts_at=draft.starts_at,
            due_at=draft.due_at,
            priority=draft.priority.value,
            status=ItemStatus.scheduled.value,
            source="bot",
        )
        session.add(item)
        await session.flush()
        await state.clear()
        await callback.message.edit_text(
            f"✅ Saved: {draft.title}", reply_markup=main_menu_kb()
        )
    await callback.answer()


@router.message(F.text)
async def on_text(
    message: Message, session: AsyncSession, state: State
) -> None:
    text = message.text or ""
    user = await _ensure_user(session, message.from_user)
    current = await state.get_state()

    if current == TaskDraftStates.waiting_for_text:
        try:
            draft = _parse_draft(text, _user_tz(user))
        except ValueError as exc:
            await message.answer(f"I couldn't read that: {exc}\n\n{DRAFT_HELP}")
            return
        await state.update_data(draft_text=text)
        await state.set_state(TaskDraftStates.confirm)
        await message.answer(
            _draft_preview(draft, _user_tz(user)), reply_markup=draft_kb()
        )
    elif current == TaskDraftStates.confirm:
        await message.answer("Please use the ✅ / ❌ buttons above.")
    elif current == SettingsStates.timezone:
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            await message.answer(f"{text!r} is not a valid IANA timezone. Try again.")
            return
        user.settings.timezone = text
        await state.clear()
        await message.answer(
            f"Timezone set to {text}.", reply_markup=settings_kb()
        )
    elif current == SettingsStates.digest_time:
        try:
            clock = _parse_hhmm(text)
        except (ValueError, TypeError):
            await message.answer("Use the 24-hour HH:MM format, e.g. 08:30.")
            return
        user.settings.digest_time = clock
        await state.clear()
        await message.answer(
            f"Digest time set to {clock:%H:%M}.", reply_markup=settings_kb()
        )
    else:
        session.add(
            ChatMessage(
                user_id=user.id,
                role=ChatRole.user.value,
                content=text,
                source="telegram",
            )
        )
        await message.answer(
            "Got it — saved. You can ask me anything about your tasks, "
            "workouts, or files.",
            reply_markup=main_menu_kb(),
        )
