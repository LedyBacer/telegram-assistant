"""Bot handlers: main menu, task drafts, settings (SPEC §5-§7).

Every user-facing string is resolved through the central i18n translator
(``assistant.i18n.t``) in the user's persisted language.
"""

from __future__ import annotations

import logging
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

from assistant.actions.calendar import ActionStaleError
from assistant.ai import AIProviderError, AITaskDraft, get_ai_provider
from assistant.ai.prompts import DRAFT_SYSTEM
from assistant.bot.callbacks import (
    ActionCallback,
    DraftCallback,
    FactCallback,
    ItemCallback,
    LanguageCallback,
    MenuCallback,
    SettingsCallback,
)
from assistant.bot.keyboards import (
    action_kb,
    draft_kb,
    fact_kb,
    items_kb,
    language_kb,
    main_menu_kb,
    settings_kb,
    workouts_kb,
)
from assistant.bot.states import SettingsStates, TaskDraftStates, WorkoutStates
from assistant.config import get_settings
from assistant.i18n import (
    DEFAULT_LANGUAGE,
    LocalizableError,
    is_supported,
    load_locale,
    t,
)
from assistant.models.calendar_items import CalendarItem, ItemKind, ItemPriority
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.models.facts import FactStatus
from assistant.models.files import FileState, UserFile
from assistant.models.users import User
from assistant.services import actions as actions_service
from assistant.services import calendar as calendar_service
from assistant.services import facts as facts_service
from assistant.services import files as files_service
from assistant.services import reminders as reminders_service
from assistant.services import turns as turns_service
from assistant.services import workouts as workouts_service
from assistant.services.users import upsert_user

router = Router(name="bot")

logger = logging.getLogger("assistant.bot")


@dataclass(slots=True)
class TaskDraft:
    """A parsed, not-yet-persisted calendar item (SPEC §6)."""

    title: str
    kind: ItemKind
    starts_at: datetime | None
    ends_at: datetime | None
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


def _user_lang(user: User) -> str:
    return user.settings.language if user.settings is not None else DEFAULT_LANGUAGE


async def _send_thinking(message: Message, lang: str) -> Message | None:
    """Send the temporary localized "Thinking…" status message.

    Only when ``CHAT_THINKING_ENABLED`` is true and an AI request is about to
    run. The message is cosmetic: a failed send (e.g. Telegram flood limit)
    must not break the request flow, so it is best-effort.
    """
    if not get_settings().chat_thinking_enabled:
        return None
    try:
        return await message.answer(t(lang, "ai.thinking"))
    except Exception:  # noqa: BLE001 — cosmetic message must never break the flow
        logger.warning("failed to send the thinking status message", exc_info=True)
        return None


async def _delete_thinking(status: Message | None) -> None:
    """Best-effort removal of the temporary status message."""
    if status is None:
        return
    try:
        await status.delete()
    except Exception:  # noqa: BLE001 — deletion failure must not break the flow
        logger.warning("failed to delete the thinking status message", exc_info=True)


def _render_validation_error(exc: ValueError, lang: str, fallback_key: str) -> str:
    """Render a validation error in the user's language.

    ``LocalizableError`` carries a locale key; any other ``ValueError`` falls
    back to a generic localized message with the raw text as ``{error}``.
    """
    if isinstance(exc, LocalizableError):
        return t(lang, exc.key, **exc.params)
    return t(lang, fallback_key, error=exc)


def _file_rejection_text(file: UserFile, lang: str) -> str:
    """Localized rejection reason for a rejected upload.

    The service stores the locale key in ``file.error`` and its parameters in
    ``file.extra["rejection"]``; legacy/free-text errors are shown verbatim.
    """
    key = file.error or ""
    if key in load_locale(DEFAULT_LANGUAGE):
        return t(lang, key, **(file.extra or {}).get("rejection", {}))
    return key


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
        ends_at=None,
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
    # SPEC §4.1: a duration extends the item (start -> end); it must NOT be
    # represented as a due date. ``due_at`` stays reserved for explicit
    # deadlines.
    ends_at: datetime | None = None
    if starts_at is not None and ai.duration_minutes:
        ends_at = starts_at + timedelta(minutes=ai.duration_minutes)
    return TaskDraft(
        title=ai.title,
        kind=ItemKind(ai.kind),
        starts_at=starts_at,
        ends_at=ends_at,
        due_at=None,
        priority=ItemPriority(ai.priority),
        description=ai.notes,
        remind_offsets=list(ai.reminder_offsets),
        ambiguities=list(ai.ambiguities),
    )


def _draft_preview(draft: TaskDraft, tz: ZoneInfo, lang: str) -> str:
    icon = "📆" if draft.kind is ItemKind.event else "✅"
    lines = [
        f"{icon} {draft.title}",
        t(lang, "draft.kind", kind=draft.kind.value),
        t(lang, "draft.priority", priority=draft.priority.value),
    ]
    if draft.starts_at:
        lines.append(
            t(
                lang,
                "draft.start",
                when=draft.starts_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                tz=tz,
            )
        )
    if draft.ends_at:
        lines.append(
            t(
                lang,
                "draft.end",
                when=draft.ends_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                tz=tz,
            )
        )
    if draft.due_at:
        lines.append(
            t(
                lang,
                "draft.due",
                when=draft.due_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                tz=tz,
            )
        )
    if draft.remind_offsets:
        offsets = ", ".join(str(o) for o in draft.remind_offsets)
        lines.append(t(lang, "draft.remind", offsets=offsets))
    if draft.description:
        lines.append(t(lang, "draft.notes", notes=draft.description))
    if draft.ambiguities:
        lines.append(t(lang, "draft.assumed", assumed="; ".join(draft.ambiguities)))
    return "\n".join(lines)


def _parse_workout_log(text: str) -> tuple[str, int | None, int | None]:
    """Parse ``name, minutes, effort`` (last two optional) for logging."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts:
        raise LocalizableError("workouts.err_name")
    name = parts[0]
    duration: int | None = None
    effort: int | None = None
    if len(parts) > 1:
        try:
            duration = int(parts[1])
        except ValueError:
            raise LocalizableError("workouts.err_minutes") from None
    if len(parts) > 2:
        try:
            effort = int(parts[2])
        except ValueError:
            raise LocalizableError("workouts.err_effort") from None
    return name, duration, effort


def _parse_workout_schedule(text: str, tz: ZoneInfo) -> tuple[str, datetime]:
    """Parse ``name, YYYY-MM-DD HH:MM`` for scheduling."""
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        raise LocalizableError("workouts.err_format")
    name = parts[0]
    try:
        when = datetime.fromisoformat(" ".join(parts[1:]))
    except ValueError:
        raise LocalizableError("workouts.err_time") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return name, when


def _fmt_items(items: list[CalendarItem], tz: ZoneInfo, lang: str) -> str:
    if not items:
        return t(lang, "tasks.empty")
    lines = []
    for item in items:
        icon = {"task": "✅", "event": "📆"}[item.kind]
        when = ""
        if item.starts_at:
            when = item.starts_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        elif item.due_at:
            when = t(
                lang,
                "tasks.item_due",
                when=item.due_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
            )
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
    await callback.answer()


@router.callback_query(DraftCallback.filter())
async def on_draft(
    callback: CallbackQuery,
    callback_data: DraftCallback,
    session: AsyncSession,
    state: State,
) -> None:
    user = await _ensure_user(session, callback.from_user)
    lang = _user_lang(user)
    if callback_data.action == "cancel":
        await state.clear()
        await callback.message.edit_text(
            t(lang, "draft.cancelled"), reply_markup=main_menu_kb(lang)
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
            ends_at=draft.ends_at,
            due_at=draft.due_at,
            priority=draft.priority,
        )
        reminders = await reminders_service.create_item_reminders(
            session, user, item, offsets_minutes=draft.remind_offsets
        )
        await state.clear()
        text = t(lang, "draft.saved", title=draft.title)
        if reminders:
            text += "\n" + t(lang, "draft.reminders", count=len(reminders))
        await callback.message.edit_text(text, reply_markup=main_menu_kb(lang))
    await callback.answer()


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
        try:
            action = await actions_service.confirm_action(
                session, user, action_id
            )
            action, _result = await actions_service.execute_action(
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
    lang = _user_lang(user)
    if file.state == FileState.rejected.value:
        await message.answer(
            t(lang, "files.rejected", error=_file_rejection_text(file, lang))
        )
    else:
        await message.answer(
            t(lang, "files.saved", filename=file.original_filename)
        )


@router.message(F.text)
async def on_text(
    message: Message, session: AsyncSession, state: State
) -> None:
    text = message.text or ""
    user = await _ensure_user(session, message.from_user)
    lang = _user_lang(user)
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
            thinking_status = await _send_thinking(message, lang)
            try:
                ai = await get_ai_provider().chat_structured(
                    system=DRAFT_SYSTEM.format(tz=tz, now=datetime.now(tz).isoformat()),
                    messages=[{"role": "user", "content": text}],
                    schema=AITaskDraft,
                )
            except AIProviderError:
                await _delete_thinking(thinking_status)
                await message.answer(
                    t(lang, "draft.failed", help=t(lang, "draft.help"))
                )
                return
            await _delete_thinking(thinking_status)
            draft = _ai_draft_to_task_draft(ai, tz)
            ai_dump = ai.model_dump(mode="json")
        await state.update_data(draft_text=text, draft_ai=ai_dump)
        await state.set_state(TaskDraftStates.confirm)
        await message.answer(
            _draft_preview(draft, tz, lang), reply_markup=draft_kb(lang)
        )
    elif current == TaskDraftStates.confirm:
        await message.answer(t(lang, "draft.confirm_prompt"))
    elif current == SettingsStates.timezone:
        try:
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            await message.answer(t(lang, "settings.tz_invalid", value=text))
            return
        user.settings.timezone = text
        await state.clear()
        await message.answer(
            t(lang, "settings.tz_set", tz=text), reply_markup=settings_kb(lang)
        )
    elif current == SettingsStates.digest_time:
        try:
            clock = _parse_hhmm(text)
        except (ValueError, TypeError):
            await message.answer(t(lang, "settings.digest_invalid"))
            return
        user.settings.digest_time = clock
        await state.clear()
        await message.answer(
            t(lang, "settings.digest_set", time=f"{clock:%H:%M}"),
            reply_markup=settings_kb(lang),
        )
    elif current == WorkoutStates.waiting_log:
        try:
            name, duration, effort = _parse_workout_log(text)
        except ValueError as exc:
            await message.answer(
                _render_validation_error(exc, lang, "workouts.cant_read")
                + "\n\n"
                + t(lang, "workouts.log_prompt")
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
            await message.answer(t(lang, "workouts.cant_read", error=exc))
            return
        await state.clear()
        await message.answer(
            t(lang, "workouts.logged", name=log.name)
            + (
                t(lang, "workouts.minutes", minutes=log.duration_minutes)
                if log.duration_minutes
                else ""
            ),
            reply_markup=workouts_kb(lang),
        )
    elif current == WorkoutStates.waiting_schedule:
        tz = _user_tz(user)
        try:
            name, when = _parse_workout_schedule(text, tz)
        except ValueError as exc:
            await message.answer(
                _render_validation_error(exc, lang, "workouts.cant_read")
                + "\n\n"
                + t(lang, "workouts.schedule_prompt")
            )
            return
        try:
            item = await workouts_service.schedule_workout(
                session, user, name=name, starts_at=when
            )
        except ValueError as exc:
            await message.answer(t(lang, "workouts.cant_schedule", error=exc))
            return
        await state.clear()
        await message.answer(
            t(
                lang,
                "workouts.scheduled",
                title=item.title,
                when=item.starts_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"),
                tz=tz,
            ),
            reply_markup=main_menu_kb(lang),
        )
    else:
        thinking_status = await _send_thinking(message, lang)
        try:
            result = await turns_service.run_turn(session, user, text)
        except AIProviderError:
            # The engine persists nothing on provider failure: keep the
            # user message and explain the outage in the user's language.
            await _delete_thinking(thinking_status)
            session.add(
                ChatMessage(
                    user_id=user.id,
                    role=ChatRole.user.value,
                    content=text,
                    source="telegram",
                )
            )
            await message.answer(
                t(lang, "errors.ai_unavailable"),
                reply_markup=main_menu_kb(lang),
            )
            return
        except LocalizableError as exc:
            # Malformed/empty model output fails safely (SPEC §2): the user
            # message is persisted and a localized fallback is shown.
            await _delete_thinking(thinking_status)
            session.add(
                ChatMessage(
                    user_id=user.id,
                    role=ChatRole.user.value,
                    content=text,
                    source="telegram",
                )
            )
            await session.flush()
            await message.answer(
                t(lang, exc.key, **exc.params),
                reply_markup=main_menu_kb(lang),
            )
            return
        await _delete_thinking(thinking_status)
        await message.answer(result.reply, reply_markup=main_menu_kb(lang))
        for action in result.proposed_actions:
            await message.answer(
                t(lang, "action.propose", summary=action.summary),
                reply_markup=action_kb(action.id, lang),
            )
        if result.skipped_actions:
            await message.answer(t(lang, "action.skipped"))
