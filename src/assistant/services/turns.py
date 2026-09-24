"""Bounded conversational action engine (SPEC §2, §3, §11).

One turn is at most **two** model calls:

1. A structured request returns a typed :class:`AssistantTurn` — a direct
   reply, read-tool data requests, proposed mutations, or a clarification.
2. Only when the model requested app data does a second (structured)
   "fold" call return the final text reply plus any mutation proposals
   that use the real ids revealed by the deterministic read-tool results
   (lookup→mutation within the bounded turn).

There is no ReAct loop: tool results are never re-offered for further tool
requests, and malformed model output fails safely (the turn degrades to a
plain reply or a localized error, never to an unbounded retry).

Mutations are never applied here: a proposal is validated against the
registered action kind's payload schema and stored as a durable
:class:`~assistant.models.pending_actions.PendingAction`; the user confirms
it, and execution happens in :mod:`assistant.services.actions`.

Formal turn state machine
-------------------------

Lifecycle::

    START
      -> CONTEXT (Phase A: bounded context, short read tx)
      -> STRUCTURED (Phase B: one structured call -> typed AssistantTurn,
                     which declares exactly one of the modes
                     answer / need_data / proposal / clarification)
      -> state classified by :func:`_classify_turn` from ``turn.mode``,
         exactly one of:

         DIRECT_REPLY    mode "answer": the reply is the assistant message.
                         Terminal.
         TOOL_FOLD       mode "need_data": run the bounded read tools,
                         then exactly ONE structured fold call (typed
                         AssistantFold) returning the final reply and any
                         mutation proposals resolved from the tool results.
                         Terminal: the folded reply is the assistant message.
         PROPOSAL        mode "proposal": the (optional) reply is the
                         assistant message; proposals-only turns persist no
                         message. Terminal.
         CLARIFICATION   mode "clarification": the question is the assistant
                         message. Terminal.

      -> PERSIST (Phase C: proposals, facts, messages, short write tx)
      -> DONE

    Any AIProviderError in Phase A/B aborts the turn: nothing from Phase C
    is committed and the caller's session stays clean for its own fallback.

Each state is terminal — a turn never re-enters the structured state, so
the engine cannot loop. The ``TurnState`` of the turn is exposed on
:class:`TurnResult` for observability.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from types import UnionType
from typing import Annotated, Literal, Union, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions import get_action_kind, registered_kinds
from assistant.ai import AIProvider, get_ai_provider
from assistant.ai.prompts import TURN_FOLD_SYSTEM, TURN_SYSTEM
from assistant.ai.schemas import (
    ActionProposal,
    AssistantFold,
    AssistantTurn,
    FactProposal,
    ReadToolRequest,
)
from assistant.i18n import DEFAULT_LANGUAGE, language_name
from assistant.models.calendar_items import CalendarItem
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.models.facts import UserFact
from assistant.models.files import UserFile
from assistant.models.pending_actions import PendingAction
from assistant.models.reminders import ReminderStatus
from assistant.models.users import User
from assistant.services import actions as actions_service
from assistant.services import calendar as calendar_service
from assistant.services import chat as chat_service
from assistant.services import facts as facts_service
from assistant.services import files as files_service
from assistant.services import reminders as reminders_service
from assistant.services import workouts as workouts_service

#: Hard bound on read tools per turn (the second model call is one-shot).
MAX_DATA_TOOLS = 3
#: Bounded excerpt limit for the documents tool (context stays small, §6.2).
DOCUMENTS_TOOL_LIMIT = 5

TOOL_DESCRIPTIONS: dict[str, str] = {
    "calendar": "calendar items (with ids). query=<item named> resolves it; "
    "range_start/range_end (YYYY-MM-DD) answer 'this month'/'next Friday'; "
    "priority=high|normal|low and kind=task|event|workout filter",
    "reminders": "pending reminders (with ids); "
    "query=<the reminder the user named> resolves it",
    "workouts": "recent workout logs",
    "files": "stored files (name, state, size)",
    "documents": "search the text of stored documents for relevant excerpts",
    "facts": "confirmed user facts (with ids)",
}


class TurnState(StrEnum):
    """Terminal state of a turn after the single structured call.

    Exactly one state per turn; see the module docstring for the full
    lifecycle. States are terminal — the engine never branches back to
    another model call except the single TOOL_FOLD fold.
    """

    DIRECT_REPLY = "direct_reply"
    TOOL_FOLD = "tool_fold"
    PROPOSAL = "proposal"
    CLARIFICATION = "clarification"


_TURN_STATE_BY_MODE: dict[str, TurnState] = {
    "answer": TurnState.DIRECT_REPLY,
    "need_data": TurnState.TOOL_FOLD,
    "proposal": TurnState.PROPOSAL,
    "clarification": TurnState.CLARIFICATION,
}


def _classify_turn(turn: AssistantTurn) -> TurnState:
    """Classify a typed turn into exactly one state (deterministic).

    The ``mode`` field is the single source of truth (V4 §12-13): the schema
    already guarantees each mode carries exactly its own fields, so this is a
    direct mapping — there is no priority order to get wrong.
    """
    return _TURN_STATE_BY_MODE[turn.mode]


@dataclass(slots=True)
class TurnResult:
    """The engine's outcome for one turn (written in Phase C and committed
    by :func:`run_turn`; the caller's final commit is a no-op)."""

    reply: str
    # Terminal state the turn settled in (see :class:`TurnState`).
    state: TurnState = TurnState.DIRECT_REPLY
    proposed_actions: list[PendingAction] = field(default_factory=list)
    # Proposals that failed re-validation and were NOT stored, with a short
    # machine reason (localized by the caller).
    skipped_actions: list[ActionProposal] = field(default_factory=list)
    # New proposed (not yet confirmed) memory facts, deduped (SPEC §14).
    proposed_facts: list[UserFact] = field(default_factory=list)
    # Chunks retrieved by the documents tool this turn; the caller renders
    # citations from them deterministically (SPEC §6.3).
    retrieved_chunks: list[files_service.RetrievedChunk] = field(default_factory=list)
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


def _parse_local_day(value: str, fallback: datetime) -> datetime:
    """Parse a ``YYYY-MM-DD`` (or full datetime) string into a naive local
    midnight datetime, falling back to ``fallback``'s date on bad input so a
    model typo cannot blow up a read tool."""
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return fallback.replace(hour=0, minute=0, second=0, microsecond=0)


async def _calendar_structured_query(
    session: AsyncSession,
    user: User,
    *,
    range_start: str | None,
    range_end: str | None,
    priority: str | None,
    kind: str | None,
) -> list:
    """Answer a date/attribute calendar question with a bounded range query
    (V4 §24).

    The window is always finite (missing bounds default to today and
    today+30d), so the underlying :func:`list_range` stays bounded by its
    own limit. ``priority`` and ``kind`` filter the (already bounded) rows in
    Python — no arbitrary SQL, no attribute index required.
    """
    tz = _user_tz(user)
    today = datetime.now(tz=tz).date()
    start = _parse_local_day(range_start, datetime(today.year, today.month, today.day))
    if range_end:
        end = _parse_local_day(range_end, datetime(today.year, today.month, today.day))
        end = end + timedelta(days=1)  # range_end is inclusive of that day
    else:
        end = datetime(today.year, today.month, today.day) + timedelta(days=30)
    if end <= start:
        end = start + timedelta(days=1)
    items = await calendar_service.list_range(session, user, start=start, end=end)
    if priority:
        items = [i for i in items if i.priority == priority]
    if kind:
        items = [i for i in items if i.kind == kind]
    return items


async def run_read_tool(
    session: AsyncSession,
    user: User,
    *,
    tool: str,
    query: str | None = None,
    limit: int = 5,
    provider: AIProvider | None = None,
    range_start: str | None = None,
    range_end: str | None = None,
    priority: str | None = None,
    kind: str | None = None,
) -> tuple[str, list[files_service.RetrievedChunk]]:
    """Run one bounded, read-only tool over the existing services.

    Returns ``(text, chunks)``: a compact text block (never raises for empty
    data — it renders "(no data)") that folds straight into a prompt, plus
    the retrieved chunks (non-empty only for the ``documents`` tool) so the
    caller can render deterministic citations (SPEC §6.3).

    ``range_start`` / ``range_end`` (``YYYY-MM-DD``), ``priority`` and
    ``kind`` are calendar-only typed parameters (V4 §24) that answer date
    and attribute questions — "what events do I have this month?", "show my
    high-priority tasks" — without a free-text resolve and without any
    unbounded scan.
    """
    if tool == "calendar":
        if range_start or range_end or priority or kind:
            items = await _calendar_structured_query(
                session,
                user,
                range_start=range_start,
                range_end=range_end,
                priority=priority,
                kind=kind,
            )
            lines = [f"{_item_line(i)}" for i in items[:limit]]
            return ("\n".join(lines) if lines else "(no data)", [])
        resolve_lines: list[str] = []
        if query:
            best, candidates = await calendar_service.resolve_item(session, user, query)
            if best is not None:
                resolve_lines.append(f"match: {_item_line(best)}")
            elif candidates:
                resolve_lines.append(
                    "ambiguous: "
                    + "; ".join(f"id={i.id} {i.title}" for i in candidates)
                )
            else:
                resolve_lines.append("match: none")
        today = (await calendar_service.list_today(session, user))[:limit]
        upcoming = (await calendar_service.list_upcoming(session, user))[:limit]
        lines = [
            f"today: {_item_line(i)}" for i in today
        ] + [f"upcoming: {_item_line(i)}" for i in upcoming]
        lines = resolve_lines + lines
        return ("\n".join(lines) if lines else "(no data)", [])
    if tool == "reminders":
        resolve_lines = []
        if query:
            best, candidates = await reminders_service.resolve_reminder(
                session, user, query
            )
            if best is not None:
                resolve_lines.append(
                    f"match: id={best.id} {best.message} at {_fmt_dt(best.fire_at)}"
                )
            elif candidates:
                resolve_lines.append(
                    "ambiguous: "
                    + "; ".join(f"id={r.id} {r.message}" for r in candidates)
                )
            else:
                resolve_lines.append("match: none")
        reminders = await reminders_service.list_reminders(
            session, user, status=ReminderStatus.pending, limit=limit
        )
        lines = [
            f"id={r.id} {r.message} at {_fmt_dt(r.fire_at)}" for r in reminders
        ]
        lines = resolve_lines + lines
        return ("\n".join(lines) if lines else "(no data)", [])
    if tool == "workouts":
        logs = await workouts_service.list_workouts(session, user, limit=limit)
        lines = [
            f"{w.name}"
            + (f", {w.duration_minutes} min" if w.duration_minutes else "")
            + f", {_fmt_dt(w.started_at)}"
            for w in logs
        ]
        return ("\n".join(lines) if lines else "(no data)", [])
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
        return ("\n".join(lines) if lines else "(no data)", [])
    if tool == "documents":
        # Hybrid retrieval (SPEC §6, §13): lexical and vector arms run
        # independently and are fused; the tool is only invoked when the
        # model requests it, so the embedding call is bounded by that choice.
        chunks = await files_service.retrieve_chunks(
            session,
            user,
            query or "",
            top_k=min(limit, DOCUMENTS_TOOL_LIMIT),
            provider=provider,
        )
        lines = [f"{c.file_name} (excerpt): {c.text[:400]}" for c in chunks]
        return ("\n".join(lines) if lines else "(no data)", list(chunks))
    if tool == "facts":
        facts = await facts_service.confirmed_facts(session, user)
        lines = [
            f"id={f.id} "
            + (f"[{f.category}] " if f.category != "general" else "")
            + f.value
            for f in facts
        ]
        return ("\n".join(f"- {line}" for line in lines) if lines else "(no data)", [])
    return ("(unknown tool)", [])


def _tools_doc() -> str:
    lines = [f"- {name}: {desc}" for name, desc in TOOL_DESCRIPTIONS.items()]
    lines.append(
        "Each request: {tool, query (optional short text, default none), "
        "limit (1..20, default 5)}. The calendar tool also accepts "
        "range_start/range_end (YYYY-MM-DD), priority, and kind."
    )
    return "\n".join(lines)


def _field_type(annotation: object) -> str:
    """Compact type descriptor for prompt docs: ``str``, ``int``, ``bool``,
    ``float``, ``datetime`` (rendered as the expected format), ``a|b|c`` for
    Literal/Enum members, and ``X[]`` for lists. None unions are unwrapped —
    optionality is conveyed by the ``?`` suffix the caller adds."""
    if annotation is type(None):
        return "any"
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        parts = [
            _field_type(arg)
            for arg in get_args(annotation)
            if arg is not type(None)
        ]
        return "|".join(parts) if parts else "any"
    if origin is Literal:
        return "|".join(str(value) for value in get_args(annotation))
    if origin is list:
        return f"{_field_type(get_args(annotation)[0])}[]"
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "|".join(str(member.value) for member in annotation)
    if annotation is datetime:
        return '"YYYY-MM-DD HH:MM"'
    if isinstance(annotation, type):
        # issubclass (not identity) so constrained types like conint render.
        if issubclass(annotation, bool):
            return "bool"
        if issubclass(annotation, int):
            return "int"
        if issubclass(annotation, float):
            return "float"
        if issubclass(annotation, str):
            return "str"
    return "any"


def _actions_doc() -> str:
    """Render the registered action kinds from the registry (single source
    of truth) so the prompt can never drift from the payload schemas.

    Field types, enum values, and the datetime format are rendered compactly
    (V3 §41) so a 9B model sees the payload contract without a full JSON
    schema dump. Fields marked ``exclude=True`` are internal to the engine
    and never offered to the model."""
    lines = []
    for kind in registered_kinds():
        spec = get_action_kind(kind)
        if spec is None:  # pragma: no cover — registry is self-consistent
            continue
        fields = ", ".join(
            f"{name}:{_field_type(f.annotation)}" + ("" if f.is_required() else "?")
            for name, f in spec.payload_schema.model_fields.items()
            if not f.exclude
        )
        lines.append(f"- {kind}({fields})")
    return "\n".join(lines)


def _dedupe_data_requests(requests: list[ReadToolRequest]) -> list[ReadToolRequest]:
    seen: set[tuple] = set()
    unique: list[ReadToolRequest] = []
    for req in requests:
        # The calendar structured params are part of the identity: two requests
        # that differ only by range/priority/kind are distinct queries (V4 §24).
        key = (
            req.tool,
            req.query,
            req.range_start,
            req.range_end,
            req.priority,
            req.kind,
        )
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
    """One bounded conversational turn (Phase A/B/C transaction layout).

    No PostgreSQL transaction (and therefore no pooled connection) is held
    while waiting on model or embedding network I/O — the same pattern the
    worker job handlers use for Telegram sends:

    - **Phase A** — short read transaction: build the bounded context, then
      ``commit()`` to release the connection.
    - **Phase B** — the structured model call, the bounded read tools, and
      the optional second (structured fold) model call, with **no open
      transaction** (each
      read tool's queries run in short transactions of their own; the
      documents tool commits before its embedding call).
    - **Phase C** — short write transaction: persist proposals, proposed
      facts, and the chat messages, then ``commit()``.

    The turn follows the formal state machine in the module docstring: the
    single structured call is classified into exactly one terminal
    :class:`TurnState` (``DIRECT_REPLY`` / ``TOOL_FOLD`` / ``PROPOSAL`` /
    ``CLARIFICATION``) from the turn's declared ``mode``; only ``TOOL_FOLD``
    triggers the one bounded second model call (typed
    :class:`~assistant.ai.schemas.AssistantFold`), and the settled state is
    exposed on the returned :class:`TurnResult`.

    Persists the user message and the assistant reply. A structurally blank
    model output is impossible (the mode validator rejects it inside the
    provider's structured call), so this function re-raises
    ``AIProviderError`` for the caller to apply its fallback; on that path
    nothing from Phase C is committed, leaving the caller's session clean.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("Chat text is required.")
    provider = provider or get_ai_provider()
    tz = _user_tz(user)
    lang = _user_lang(user)

    # Phase A: short read transaction.
    ctx = await chat_service.build_context(session, user, text, provider=provider)
    history = [
        {"role": m.role, "content": m.content}
        for m in ctx.recent_messages
        if m.role in (ChatRole.user.value, ChatRole.assistant.value)
    ]
    history.append({"role": ChatRole.user.value, "content": text})
    context_block = chat_service.render_context(ctx)
    await session.commit()  # release the connection before any model I/O

    # Phase B: model calls and read tools with no open transaction.
    turn = await provider.chat_structured(
        system=TURN_SYSTEM.format(
            context=context_block,
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
    state = _classify_turn(turn)

    reply = turn.reply
    clarification = turn.clarification
    fold_actions: list[ActionProposal] = []
    fold_facts: list[FactProposal] = []
    retrieved: list[files_service.RetrievedChunk] = []
    if state is TurnState.TOOL_FOLD:
        # The only state that makes the second (fold) model call; the
        # other states are terminal after the structured call. The fold
        # is STRUCTURED, so a lookup can be followed by a mutation
        # proposal that uses the real ids revealed by the tool results
        # (lookup→mutation within the bounded two-call turn).
        blocks = []
        for req in _dedupe_data_requests(turn.data_requests):
            tool_text, chunks = await run_read_tool(
                session,
                user,
                tool=req.tool,
                query=req.query,
                limit=req.limit,
                provider=provider,
                range_start=req.range_start,
                range_end=req.range_end,
                priority=req.priority,
                kind=req.kind,
            )
            blocks.append(f"[{req.tool}]\n{tool_text}")
            for chunk in chunks:
                if not any(
                    r.file_id == chunk.file_id and r.position == chunk.position
                    for r in retrieved
                ):
                    retrieved.append(chunk)
        await session.commit()  # release before the second model call
        fold = await provider.chat_structured(
            system=TURN_FOLD_SYSTEM.format(
                tool_results="\n\n".join(blocks) if blocks else "(none)",
                actions_doc=_actions_doc(),
                language=language_name(lang),
                tz=str(tz),
                now=datetime.now(tz).isoformat(),
            ),
            messages=history,
            schema=AssistantFold,
        )
        # The fold is the last word: its schema (AssistantFold) has no
        # need_data mode, so it structurally cannot request more data and
        # the turn never loops (V4 §14-15).
        reply = fold.reply
        clarification = fold.clarification or turn.clarification
        fold_actions = list(fold.actions)
        fold_facts = list(fold.facts)
        model_calls = 2

    # Phase C: short write transaction.
    proposed: list[PendingAction] = []
    skipped: list[ActionProposal] = []
    for proposal in [*turn.actions, *fold_actions]:
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

    proposed_facts: list[UserFact] = []
    for fact in [*turn.facts, *fold_facts]:
        # ``replaces_fact_id`` is a model-referenced fact id (SPEC §16); it is
        # revalidated for ownership + state inside ``propose_if_absent`` and
        # silently dropped if it is not a live fact of this user.
        created = await facts_service.propose_if_absent(
            session,
            user,
            value=fact.value,
            category=fact.category,
            provenance="assistant",
            replaces_fact_id=fact.replaces_fact_id,
        )
        if created is not None:
            proposed_facts.append(created)

    # The final assistant message is the mode's own text (V4 §12-13): an
    # answer's reply, a proposal's reply (proposals-only turns persist no
    # message), or a clarification's question. A valid turn always names a
    # mode with its field populated, so there is no blank-turn path.
    if state in (TurnState.DIRECT_REPLY, TurnState.PROPOSAL):
        final = reply
    else:  # TOOL_FOLD (fold already set reply) and CLARIFICATION
        final = reply if reply and reply.strip() else clarification
    if final is None:
        final = ""

    session.add(
        ChatMessage(
            user_id=user.id,
            role=ChatRole.user.value,
            content=text,
            source="telegram",
        )
    )
    if final:
        session.add(
            ChatMessage(
                user_id=user.id,
                role=ChatRole.assistant.value,
                content=final,
                source="ai",
            )
        )
    await session.commit()
    return TurnResult(
        reply=final,
        state=state,
        proposed_actions=proposed,
        skipped_actions=skipped,
        proposed_facts=proposed_facts,
        retrieved_chunks=retrieved,
        model_calls=model_calls,
    )
