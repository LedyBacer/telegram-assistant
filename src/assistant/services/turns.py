"""Bounded conversational action engine (SPEC §2, §3, §11).

One turn is at most **two** model calls:

1. A structured request returns a typed :class:`AssistantTurn` — a direct
   reply, read-tool data requests, proposed mutations, or a clarification.
2. Only when the model requested app data does a second (plain) call fold
   the deterministic read-tool results into a final text reply.

There is no ReAct loop: tool results are never re-offered for further tool
requests, and malformed model output fails safely (the turn degrades to a
plain reply or a localized error, never to an unbounded retry).

Mutations are never applied here: a proposal is validated against the
registered action kind's payload schema and stored as a durable
:class:`~assistant.models.pending_actions.PendingAction`; the user confirms
it, and execution happens in :mod:`assistant.services.actions`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions import get_action_kind, registered_kinds
from assistant.ai import AIProvider, get_ai_provider
from assistant.ai.prompts import TURN_FINAL_SYSTEM, TURN_SYSTEM
from assistant.ai.schemas import ActionProposal, AssistantTurn, ReadToolRequest
from assistant.i18n import DEFAULT_LANGUAGE, LocalizableError, language_name
from assistant.models.calendar_items import CalendarItem
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.models.files import UserFile
from assistant.models.pending_actions import PendingAction
from assistant.models.reminders import ReminderStatus
from assistant.models.users import User
from assistant.services import actions as actions_service
from assistant.services import calendar as calendar_service
from assistant.services import chat as chat_service
from assistant.services import facts as facts_service
from assistant.services import reminders as reminders_service
from assistant.services import workouts as workouts_service

#: Hard bound on read tools per turn (the second model call is one-shot).
MAX_DATA_TOOLS = 3

TOOL_DESCRIPTIONS: dict[str, str] = {
    "calendar": "today's and upcoming calendar items (with ids)",
    "reminders": "pending reminders (with ids)",
    "workouts": "recent workout logs",
    "files": "stored files (name, state, size)",
    "facts": "confirmed user facts",
}


@dataclass(slots=True)
class TurnResult:
    """The engine's outcome for one turn (flush only; caller commits)."""

    reply: str
    proposed_actions: list[PendingAction] = field(default_factory=list)
    # Proposals that failed re-validation and were NOT stored, with a short
    # machine reason (localized by the caller).
    skipped_actions: list[ActionProposal] = field(default_factory=list)
    model_calls: int = 0


def _user_tz(user: User) -> ZoneInfo:
    name = user.settings.timezone if user.settings is not None else "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _user_lang(user: User) -> str:
    return user.settings.language if user.settings is not None else DEFAULT_LANGUAGE


def _fmt_dt(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "no time"


def _item_line(item: CalendarItem) -> str:
    return (
        f"id={item.id} {item.title} [{item.kind}]"
        + (f", starts {_fmt_dt(item.starts_at)}" if item.starts_at else "")
        + (f", due {_fmt_dt(item.due_at)}" if item.due_at else "")
    )


async def run_read_tool(
    session: AsyncSession,
    user: User,
    *,
    tool: str,
    query: str | None = None,
    limit: int = 5,
) -> str:
    """Run one bounded, read-only tool over the existing services.

    Returns a compact text block (never raises for empty data — it renders
    "(no data)") so the result can be folded straight into a prompt.
    """
    if tool == "calendar":
        today = (await calendar_service.list_today(session, user))[:limit]
        upcoming = (await calendar_service.list_upcoming(session, user))[:limit]
        lines = [
            f"today: {_item_line(i)}" for i in today
        ] + [f"upcoming: {_item_line(i)}" for i in upcoming]
        return "\n".join(lines) if lines else "(no data)"
    if tool == "reminders":
        reminders = await reminders_service.list_reminders(
            session, user, status=ReminderStatus.pending, limit=limit
        )
        lines = [
            f"id={r.id} {r.message} at {_fmt_dt(r.fire_at)}" for r in reminders
        ]
        return "\n".join(lines) if lines else "(no data)"
    if tool == "workouts":
        logs = await workouts_service.list_workouts(session, user, limit=limit)
        lines = [
            f"{w.name}"
            + (f", {w.duration_minutes} min" if w.duration_minutes else "")
            + f", {_fmt_dt(w.started_at)}"
            for w in logs
        ]
        return "\n".join(lines) if lines else "(no data)"
    if tool == "files":
        files = (
            (
                await session.execute(
                    select(UserFile)
                    .where(UserFile.user_id == user.id)
                    .order_by(UserFile.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        lines = [
            f"{f.original_filename} (state={f.state.value}, {f.size_bytes} bytes)"
            for f in files
        ]
        return "\n".join(lines) if lines else "(no data)"
    if tool == "facts":
        lines = await facts_service.confirmed_lines(session, user)
        return "\n".join(f"- {line}" for line in lines) if lines else "(no data)"
    return "(unknown tool)"


def _tools_doc() -> str:
    return "\n".join(f"- {name}: {desc}" for name, desc in TOOL_DESCRIPTIONS.items())


def _actions_doc() -> str:
    """Render the registered action kinds from the registry (single source
    of truth) so the prompt can never drift from the payload schemas."""
    lines = []
    for kind in registered_kinds():
        spec = get_action_kind(kind)
        if spec is None:  # pragma: no cover — registry is self-consistent
            continue
        fields = ", ".join(
            name + ("" if f.is_required() else "?")
            for name, f in spec.payload_schema.model_fields.items()
        )
        lines.append(f"- {kind}({fields})")
    return "\n".join(lines)


def _dedupe_data_requests(requests: list[ReadToolRequest]) -> list[ReadToolRequest]:
    seen: set[tuple[str, str | None]] = set()
    unique: list[ReadToolRequest] = []
    for req in requests:
        key = (req.tool, req.query)
        if key in seen:
            continue
        seen.add(key)
        unique.append(req)
    return unique[:MAX_DATA_TOOLS]


async def run_turn(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    provider: AIProvider | None = None,
) -> TurnResult:
    """One bounded conversational turn (flush only; caller owns the commit).

    Persists the user message and the assistant reply. Raises
    :class:`LocalizableError` (``chat.empty_turn``) when the model produced
    no usable output, and re-raises ``AIProviderError`` so the caller can
    apply its fallback.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Chat text is required.")
    provider = provider or get_ai_provider()
    tz = _user_tz(user)
    lang = _user_lang(user)

    ctx = await chat_service.build_context(session, user, text, provider=provider)
    history = [
        {"role": m.role, "content": m.content}
        for m in ctx.recent_messages
        if m.role in (ChatRole.user.value, ChatRole.assistant.value)
    ]
    history.append({"role": ChatRole.user.value, "content": text})

    turn = await provider.chat_structured(
        system=TURN_SYSTEM.format(
            context=chat_service.render_context(ctx),
            actions_doc=_actions_doc(),
            tools_doc=_tools_doc(),
            language=language_name(lang),
            tz=str(tz),
            now=datetime.now(tz).isoformat(),
        ),
        messages=history,
        schema=AssistantTurn,
    )
    model_calls = 1

    reply = turn.reply
    if turn.data_requests and not reply:
        blocks = [
            f"[{req.tool}]\n{await run_read_tool(session, user, tool=req.tool, query=req.query, limit=req.limit)}"
            for req in _dedupe_data_requests(turn.data_requests)
        ]
        reply = await provider.chat(
            system=TURN_FINAL_SYSTEM.format(
                tool_results="\n\n".join(blocks) if blocks else "(none)",
                language=language_name(lang),
            ),
            messages=history,
        )
        model_calls = 2

    proposed: list[PendingAction] = []
    skipped: list[ActionProposal] = []
    for proposal in turn.actions:
        spec = get_action_kind(proposal.kind)
        if spec is None:
            skipped.append(proposal)
            continue
        try:
            parsed = spec.payload_schema.model_validate(proposal.payload)
        except ValidationError:
            skipped.append(proposal)
            continue
        proposed.append(
            await actions_service.propose_action(
                session, user, kind=proposal.kind, payload=parsed, summary=proposal.summary
            )
        )

    final = reply if reply and reply.strip() else turn.clarification
    if final is None:
        raise LocalizableError("chat.empty_turn")

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
            content=final,
            source="ai",
        )
    )
    await session.flush()
    return TurnResult(
        reply=final,
        proposed_actions=proposed,
        skipped_actions=skipped,
        model_calls=model_calls,
    )
