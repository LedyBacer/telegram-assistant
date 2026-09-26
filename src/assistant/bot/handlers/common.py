"""Shared helpers for the bot's domain handlers (SPEC §5-§7).

Pure parsing/formatting utilities, the ``TaskDraft`` value type, the shared
``assistant.bot`` logger, and the private-chats-only guard router. Kept in one
place so every per-domain handler module behaves identically and the guard can
be exported alongside the composed router.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AITaskDraft
from assistant.i18n import DEFAULT_LANGUAGE, LocalizableError, load_locale, t
from assistant.models.calendar_items import CalendarItem, ItemKind, ItemPriority
from assistant.models.files import UserFile
from assistant.models.users import User
from assistant.services import reminders as reminders_service
from assistant.services.users import upsert_user

# --- Private-chats-only guard (V3 P25) ------------------------------------
# The data model uses the Telegram user ID as the background-delivery chat
# ID (reminders/digests are sent to the user's private chat). Group and
# channel usage is unsupported: the main router's filters keep every bot
# handler private-only, and this guard router — included in the dispatcher
# *before* the main router — explains the restriction to the sender. No
# user row, item, reminder, or FSM state is ever created from a group.
private_guard = Router(name="private_guard")


@private_guard.message(F.chat.type != "private")
async def reject_non_private_chat(
    message: Message, session: AsyncSession
) -> None:
    lang = await _lookup_user_lang(session, message.from_user)
    await message.answer(t(lang, "chat.private_only"))


@private_guard.callback_query(F.message.chat.type != "private")
async def reject_non_private_callback(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    lang = await _lookup_user_lang(session, callback.from_user)
    await callback.answer(t(lang, "chat.private_only"), show_alert=True)


async def _lookup_user_lang(session: AsyncSession, tg_user: Any) -> str:
    """Language of an *existing* user, without upserting (the rejection
    path must not create users). Falls back to the default language."""
    user = (await session.scalars(select(User).where(User.id == tg_user.id))).one_or_none()
    if user is None or user.settings is None:
        return DEFAULT_LANGUAGE
    return user.settings.language


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
        # Shared SPEC §14.2 bounds/dedup/max gate (out-of-bounds offsets
        # fall through to the AI draft path like any other parse error).
        remind_offsets = reminders_service.validate_reminder_offsets(remind_offsets)

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
