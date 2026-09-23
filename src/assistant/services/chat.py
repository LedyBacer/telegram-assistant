"""Contextual AI chat (SPEC §15).

Context is built *selectively* per turn — recent messages, today's and
upcoming tasks, pending reminders, recently touched entities (items and
reminders that fall outside those windows, so follow-up references to them
resolve without a tool round-trip), recent workouts, and confirmed facts —
never the whole database. Document retrieval is conditional (SPEC §6): it is
only performed when the orchestration layer explicitly requests it, so
ordinary chat never calls the embedding provider. Document excerpts and
stored facts are untrusted data: they are rendered into the system prompt as
context only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProvider, get_ai_provider
from assistant.ai.prompts import CHAT_SYSTEM
from assistant.config import get_settings
from assistant.i18n import DEFAULT_LANGUAGE, language_name
from assistant.models.calendar_items import CalendarItem
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.models.reminders import Reminder, ReminderStatus
from assistant.models.users import User
from assistant.models.workout_logs import WorkoutLog
from assistant.services import calendar as calendar_service
from assistant.services import facts as facts_service
from assistant.services import files as files_service
from assistant.services import reminders as reminders_service
from assistant.services import workouts as workouts_service

TASK_CONTEXT_LIMIT = 5
REMINDER_CONTEXT_LIMIT = 5
WORKOUT_CONTEXT_LIMIT = 5
FILE_CHUNK_CONTEXT_LIMIT = 3
RECENT_ITEM_CONTEXT_LIMIT = 5
RECENT_REMINDER_CONTEXT_LIMIT = 3


@dataclass(slots=True)
class ChatContext:
    """The selectively-assembled context for one chat turn."""

    recent_messages: list[ChatMessage] = field(default_factory=list)
    today_items: list[CalendarItem] = field(default_factory=list)
    upcoming_items: list[CalendarItem] = field(default_factory=list)
    reminders: list[Reminder] = field(default_factory=list)
    workouts: list[WorkoutLog] = field(default_factory=list)
    # Entities the user most recently created/updated that fall outside the
    # today / next-7-days windows above, so follow-up references to them
    # ("move it", "cancel that") can resolve without a tool round-trip.
    recent_items: list[CalendarItem] = field(default_factory=list)
    recent_reminders: list[Reminder] = field(default_factory=list)
    fact_lines: list[str] = field(default_factory=list)
    file_excerpts: list[str] = field(default_factory=list)
    citations: str = ""


def _fmt_dt(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "no time"


def _item_line(item: CalendarItem) -> str:
    # The id is rendered so the model can reference the entity in follow-up
    # turns (e.g. "move it to 20:00" -> update_item payload, SPEC §3).
    return (
        f"id={item.id} {item.title} [{item.kind}]"
        + (f", starts {_fmt_dt(item.starts_at)}" if item.starts_at else "")
        + (f", due {_fmt_dt(item.due_at)}" if item.due_at else "")
    )


async def build_context(
    session: AsyncSession,
    user: User,
    query: str,
    *,
    provider: AIProvider | None = None,
    retrieve: bool = False,
) -> ChatContext:
    """Assemble the bounded context block for one turn (read-only).

    ``retrieve=False`` (the default) means no file retrieval and therefore no
    embedding request (SPEC §6); pass ``retrieve=True`` only when document
    context is genuinely wanted for this turn.
    """
    settings = get_settings()
    recent = (
        await session.scalars(
            select(ChatMessage)
            .where(ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .limit(settings.chat_history_messages)
        )
    ).all()

    ctx = ChatContext(recent_messages=list(reversed(recent)))
    ctx.today_items = (await calendar_service.list_today(session, user))[:TASK_CONTEXT_LIMIT]
    ctx.upcoming_items = (
        await calendar_service.list_upcoming(session, user)
    )[:TASK_CONTEXT_LIMIT]
    ctx.reminders = (
        await reminders_service.list_reminders(
            session, user, status=ReminderStatus.pending, limit=REMINDER_CONTEXT_LIMIT
        )
    )
    ctx.workouts = await workouts_service.list_workouts(
        session, user, limit=WORKOUT_CONTEXT_LIMIT
    )
    known_item_ids = {i.id for i in ctx.today_items} | {
        i.id for i in ctx.upcoming_items
    }
    recent_items = (
        await session.scalars(
            select(CalendarItem)
            .where(CalendarItem.user_id == user.id)
            .order_by(CalendarItem.updated_at.desc(), CalendarItem.id.desc())
            .limit(TASK_CONTEXT_LIMIT + RECENT_ITEM_CONTEXT_LIMIT)
        )
    ).all()
    ctx.recent_items = [
        i for i in recent_items if i.id not in known_item_ids
    ][:RECENT_ITEM_CONTEXT_LIMIT]
    known_reminder_ids = {r.id for r in ctx.reminders}
    recent_reminders = (
        await session.scalars(
            select(Reminder)
            .where(
                Reminder.user_id == user.id,
                Reminder.status == ReminderStatus.pending.value,
            )
            .order_by(Reminder.created_at.desc(), Reminder.id.desc())
            .limit(REMINDER_CONTEXT_LIMIT + RECENT_REMINDER_CONTEXT_LIMIT)
        )
    ).all()
    ctx.recent_reminders = [
        r for r in recent_reminders if r.id not in known_reminder_ids
    ][:RECENT_REMINDER_CONTEXT_LIMIT]
    ctx.fact_lines = await facts_service.confirmed_lines(session, user)

    if retrieve:
        chunks = await files_service.retrieve_chunks(
            session, user, query, top_k=FILE_CHUNK_CONTEXT_LIMIT, provider=provider
        )
        ctx.file_excerpts = [
            f"{c.file_name} (excerpt): {c.text[:400]}" for c in chunks
        ]
        ctx.citations = files_service.format_citations(chunks)
    return ctx


def render_context(ctx: ChatContext) -> str:
    """Render the context block for the system prompt. Every value rendered
    here is user data, treated as untrusted (SPEC §14-15)."""
    sections: list[str] = []
    if ctx.fact_lines:
        sections.append("Confirmed facts about the user:\n- " + "\n- ".join(ctx.fact_lines))
    if ctx.today_items:
        sections.append("Today's tasks/events:\n- " + "\n- ".join(_item_line(i) for i in ctx.today_items))
    if ctx.upcoming_items:
        sections.append("Upcoming (7 days):\n- " + "\n- ".join(_item_line(i) for i in ctx.upcoming_items))
    if ctx.reminders:
        sections.append(
            "Pending reminders:\n- "
            + "\n- ".join(
                f"id={r.id} {r.message} at {_fmt_dt(r.fire_at)}" for r in ctx.reminders
            )
        )
    if ctx.recent_items:
        sections.append(
            "Recently touched items (outside the windows above):\n- "
            + "\n- ".join(
                _item_line(i) + f", status: {i.status}" for i in ctx.recent_items
            )
        )
    if ctx.recent_reminders:
        sections.append(
            "Recently created reminders:\n- "
            + "\n- ".join(
                f"id={r.id} {r.message} at {_fmt_dt(r.fire_at)}"
                for r in ctx.recent_reminders
            )
        )
    if ctx.workouts:
        sections.append(
            "Recent workouts:\n- "
            + "\n- ".join(
                f"{w.name}"
                + (f", {w.duration_minutes} min" if w.duration_minutes else "")
                + f", {_fmt_dt(w.started_at)}"
                for w in ctx.workouts
            )
        )
    if ctx.file_excerpts:
        sections.append("Relevant file excerpts:\n- " + "\n- ".join(ctx.file_excerpts))
        if ctx.citations:
            sections.append(ctx.citations)
    return "\n\n".join(sections) if sections else "(no additional context)"


async def chat(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    provider: AIProvider | None = None,
) -> str:
    """One conversational turn (Phase A/B/C transaction layout).

    Builds the context and ``commit()``s (Phase A) so no transaction spans
    the model network call (Phase B), then persists both messages in a
    short write transaction and ``commit()``s (Phase C).

    On provider failure nothing is persisted here and the AIProviderError is
    re-raised; the caller decides the fallback (e.g. persist the user message
    itself and reply with an error message).
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Chat text is required.")
    provider = provider or get_ai_provider()
    ctx = await build_context(session, user, text, provider=provider)

    history = [
        {"role": m.role, "content": m.content}
        for m in ctx.recent_messages
        if m.role in (ChatRole.user.value, ChatRole.assistant.value)
    ]
    history.append({"role": ChatRole.user.value, "content": text})

    lang = (
        user.settings.language
        if user.settings is not None
        else DEFAULT_LANGUAGE
    )
    system = CHAT_SYSTEM.format(
        context=render_context(ctx),
        language=language_name(lang),
    )
    await session.commit()  # Phase A: release the connection before model I/O

    reply = await provider.chat(system=system, messages=history)

    session.add(
        ChatMessage(
            user_id=user.id,
            role=ChatRole.user.value,
            content=text,
            source="telegram",
        )
    )
    session.add(
        ChatMessage(
            user_id=user.id,
            role=ChatRole.assistant.value,
            content=reply,
            source="ai",
        )
    )
    await session.commit()
    return reply
