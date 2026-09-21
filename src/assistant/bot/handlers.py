"""Bot handlers: main menu, task drafts, settings (SPEC §5-§7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProviderError, AITaskDraft, get_ai_provider
from assistant.ai.prompts import DRAFT_SYSTEM
from assistant.bot.callbacks import (
    DraftCallback,
    ItemCallback,
    MenuCallback,
    SettingsCallback,
)
from assistant.bot.keyboards import (
    draft_kb,
    items_kb,
    main_menu_kb,
    settings_kb,
    workouts_kb,
)
from assistant.bot.states import SettingsStates, TaskDraftStates, WorkoutStates
from assistant.config import get_settings
from assistant.models.calendar_items import CalendarItem, ItemKind, ItemPriority
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.models.files import FileState, UserFile
from assistant.models.users import User
from assistant.services import calendar as calendar_service
from assistant.services import files as files_service
from assistant.services import reminders as reminders_service
from assistant.services import workouts as workouts_service
from assistant.services.users import upsert_user

router = Router(name="bot")

DRAFT_HELP = (
    "Just describe the task in natural language, e.g.\n"
    "\"Tomorrow at 18:30 remind me to call Alex\".\n\n"
    "You can also send the structured line format:\n"
    "title: Buy groceries\n"
    "date: 2026-09-21\n"
    "time: 18:30\n"
    "due: 2026-09-21 19:00\n"
    "priority: high\n"
    "kind: event\n"
    "remind: -30, 0\n"
    "description: milk, bread\n"
    "`remind:` is optional — minutes before the start time (0 = at the\n"
    "start, negative = after). Only `title` is required. /cancel aborts."
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
    remind_offsets: list[int]
    ambiguities: list[str] = field(default_factory=list)


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

    remind_offsets: list[int] = []
    if raw_remind := fields.get("remind"):
        for part in raw_remind.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                remind_offsets.append(int(part))
            except ValueError:
                raise ValueError("remind: must be comma-separated integers.") from None
        if not remind_offsets:
            raise ValueError("remind: must list at least one offset.")

    return TaskDraft(
        title=title,
        kind=kind,
        starts_at=starts_at,
        due_at=due_at,
        priority=priority,
        description=fields.get("description") or None,
        remind_offsets=remind_offsets,
    )


def _ai_draft_to_task_draft(ai: AITaskDraft, tz: ZoneInfo) -> TaskDraft:
    """Convert a model-produced draft into the internal TaskDraft (UTC)."""
    starts_at: datetime | None = None
    if ai.start is not None:
        starts_at = ai.start
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=tz)
        starts_at = starts_at.astimezone(UTC)
    due_at: datetime | None = None
    if starts_at is not None and ai.duration_minutes:
        due_at = starts_at + timedelta(minutes=ai.duration_minutes)
    return TaskDraft(
        title=ai.title,
        kind=ItemKind(ai.kind),
        starts_at=starts_at,
        due_at=due_at,
        priority=ItemPriority(ai.priority),
        description=ai.notes,
        remind_offsets=list(ai.reminder_offsets),
        ambiguities=list(ai.ambiguities),
    )


def _draft_preview(draft: TaskDraft, tz: ZoneInfo) -> str:
    icon = "📆" if draft.kind is ItemKind.event else "✅"
    lines = [f"{icon} {draft.title}", f"kind: {draft.kind.value}", f"priority: {draft.priority.value}"]
    if draft.starts_at:
        lines.append(f"start: {draft.starts_at.astimezone(tz):%Y-%m-%d %H:%M} ({tz})")
    if draft.due_at:
        lines.append(f"due: {draft.due_at.astimezone(tz):%Y-%m-%d %H:%M} ({tz})")
    if draft.remind_offsets:
        lines.append(f"remind: {', '.join(str(o) for o in draft.remind_offsets)} min before start")
    if draft.description:
        lines.append(f"notes: {draft.description}")
    if draft.ambiguities:
        lines.append("assumed: " + "; ".join(draft.ambiguities))
    return "\n".join(lines)


def _parse_workout_log(text: str) -> tuple[str, int | None, int | None]:
    """Parse ``name, minutes, effort`` (last two optional) for logging."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise ValueError("Workout name is required.")
    name = parts[0]
    duration: int | None = None
    effort: int | None = None
    if len(parts) > 1:
        try:
            duration = int(parts[1])
        except ValueError:
            raise ValueError("Minutes must be a whole number.") from None
    if len(parts) > 2:
        try:
            effort = int(parts[2])
        except ValueError:
            raise ValueError("Effort must be a whole number 1-10.") from None
    return name, duration, effort


def _parse_workout_schedule(text: str, tz: ZoneInfo) -> tuple[str, datetime]:
    """Parse ``name, YYYY-MM-DD HH:MM`` for scheduling."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        raise ValueError("Send: name, YYYY-MM-DD HH:MM")
    name = parts[0]
    try:
        when = datetime.fromisoformat(" ".join(parts[1:]))
    except ValueError:
        raise ValueError("Time must look like 2026-09-23 18:00.") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return name, when


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

    if section == "main":
        await callback.message.edit_text(
            "Main menu:", reply_markup=main_menu_kb()
        )
    elif section == "task":
        await state.set_state(TaskDraftStates.waiting_for_text)
        await callback.message.edit_text(DRAFT_HELP)
    elif section == "today":
        day = datetime.now(tz=tz).date()
        items = await calendar_service.list_today(session, user)
        await callback.message.edit_text(
            f"📅 Today ({day.isoformat()}):\n{_fmt_items(items, tz)}",
            reply_markup=items_kb(items) if items else main_menu_kb(),
        )
    elif section == "upcoming":
        items = await calendar_service.list_upcoming(session, user)
        await callback.message.edit_text(
            f"📆 Next 7 days:\n{_fmt_items(items, tz)}",
            reply_markup=items_kb(items) if items else main_menu_kb(),
        )
    elif section == "workouts":
        stats = await workouts_service.workout_stats(session, user)
        logs = await workouts_service.list_workouts(session, user, limit=5)
        lines = [
            f"💪 Total: {stats['total']} · this week: {stats['this_week']} · "
            f"streak: {stats['current_streak']}d (best {stats['longest_streak']}d)"
        ]
        if logs:
            lines.append("Recent:")
            lines.extend(
                f"🏋️ {log.started_at.astimezone(tz):%Y-%m-%d %H:%M}  {log.name}"
                + (f" ({log.duration_minutes} min)" if log.duration_minutes else "")
                for log in logs
            )
        else:
            lines.append("No workouts logged yet.")
        await callback.message.edit_text("\n".join(lines), reply_markup=workouts_kb())
    elif section == "log_workout":
        await state.set_state(WorkoutStates.waiting_log)
        await callback.message.edit_text(
            "Send: name, minutes, effort (1-10) — the last two are optional.\n"
            "Example: Running, 30, 7\n/cancel aborts."
        )
    elif section == "schedule_workout":
        await state.set_state(WorkoutStates.waiting_schedule)
        await callback.message.edit_text(
            "Send: name, YYYY-MM-DD HH:MM\n"
            "Example: Running, 2026-09-23 18:00\n/cancel aborts."
        )
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
            due_at=draft.due_at,
            priority=draft.priority,
        )
        reminders = await reminders_service.create_item_reminders(
            session, user, item, offsets_minutes=draft.remind_offsets
        )
        await state.clear()
        text = f"✅ Saved: {draft.title}"
        if reminders:
            text += f"\n⏰ {len(reminders)} reminder(s) scheduled."
        await callback.message.edit_text(text, reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(ItemCallback.filter())
async def on_item(
    callback: CallbackQuery,
    callback_data: ItemCallback,
    session: AsyncSession,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    tz = _user_tz(user)
    if callback_data.action == "complete":
        item = await calendar_service.complete_item(
            session, user, callback_data.item_id
        )
        if item is None:
            text = "That item is no longer available."
        else:
            # A completed item's pending reminders are no longer useful.
            await reminders_service.cancel_item_reminders(
                session, user, item.id
            )
            when = (
                f" (was {item.starts_at.astimezone(tz):%Y-%m-%d %H:%M})"
                if item.starts_at
                else ""
            )
            text = f"✅ Done: {item.title}{when}"
    elif callback_data.action == "cancel":
        item = await calendar_service.cancel_item(
            session, user, callback_data.item_id
        )
        text = (
            f"🚫 Cancelled: {item.title}"
            if item is not None
            else "That item is no longer available."
        )
    else:
        text = "Main menu:"
    await callback.message.edit_text(text, reply_markup=main_menu_kb())
    await callback.answer()


@router.message(F.document)
async def on_document(message: Message, session: AsyncSession) -> None:
    user = await _ensure_user(session, message.from_user)
    doc = message.document
    if doc is None:
        return
    file = await files_service.register_upload(
        session,
        user,
        original_filename=doc.file_name,
        mime_type=doc.mime_type or "",
        size_bytes=doc.file_size,
        telegram_file_id=doc.file_id,
        telegram_file_unique_id=doc.file_unique_id,
    )
    if file.state == FileState.rejected.value:
        await message.answer(f"🚫 Couldn't store that file: {file.error}")
    else:
        await message.answer(
            f"📄 Saved {file.original_filename}. I'm indexing it in the "
            "background — check 📁 My files for the status."
        )


@router.message(F.text)
async def on_text(
    message: Message, session: AsyncSession, state: State
) -> None:
    text = message.text or ""
    user = await _ensure_user(session, message.from_user)
    current = await state.get_state()

    if current == TaskDraftStates.waiting_for_text:
        tz = _user_tz(user)
        draft: TaskDraft | None = None
        ai_dump: dict[str, Any] | None = None
        try:
            draft = _parse_draft(text, tz)
        except ValueError:
            # Natural language: let the model produce a typed draft. The
            # manual format stays available as a deterministic fallback.
            try:
                ai = await get_ai_provider().chat_structured(
                    system=DRAFT_SYSTEM.format(tz=tz, now=datetime.now(tz).isoformat()),
                    messages=[{"role": "user", "content": text}],
                    schema=AITaskDraft,
                )
            except AIProviderError:
                await message.answer(
                    "I couldn't interpret that. Try rephrasing, or use the\n"
                    f"structured format.\n\n{DRAFT_HELP}"
                )
                return
            draft = _ai_draft_to_task_draft(ai, tz)
            ai_dump = ai.model_dump(mode="json")
        await state.update_data(draft_text=text, draft_ai=ai_dump)
        await state.set_state(TaskDraftStates.confirm)
        await message.answer(_draft_preview(draft, tz), reply_markup=draft_kb())
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
    elif current == WorkoutStates.waiting_log:
        try:
            name, duration, effort = _parse_workout_log(text)
        except ValueError as exc:
            await message.answer(
                f"I couldn't read that: {exc}\n\n"
                "Send: name, minutes, effort (1-10). /cancel aborts."
            )
            return
        try:
            log = await workouts_service.log_workout(
                session,
                user,
                name=name,
                duration_minutes=duration,
                perceived_effort=effort,
            )
        except ValueError as exc:
            await message.answer(f"I couldn't read that: {exc}")
            return
        await state.clear()
        await message.answer(
            f"🏋️ Logged: {log.name}"
            + (f" ({log.duration_minutes} min)" if log.duration_minutes else ""),
            reply_markup=workouts_kb(),
        )
    elif current == WorkoutStates.waiting_schedule:
        tz = _user_tz(user)
        try:
            name, when = _parse_workout_schedule(text, tz)
        except ValueError as exc:
            await message.answer(
                f"I couldn't read that: {exc}\n\n"
                "Send: name, YYYY-MM-DD HH:MM. /cancel aborts."
            )
            return
        try:
            item = await workouts_service.schedule_workout(
                session, user, name=name, starts_at=when
            )
        except ValueError as exc:
            await message.answer(f"I couldn't schedule that: {exc}")
            return
        await state.clear()
        await message.answer(
            f"📅 Scheduled: {item.title} at "
            f"{item.starts_at.astimezone(tz):%Y-%m-%d %H:%M} ({tz}).\n"
            "You'll get a reminder at the start time.",
            reply_markup=main_menu_kb(),
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
