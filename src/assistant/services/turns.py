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
      -> STRUCTURED (Phase B: one structured call -> typed AssistantTurn)
      -> state classified by :func:`_classify_turn`, exactly one of:

         TOOL_FOLD     run the bounded read tools, then exactly ONE
                       structured fold call returning the final reply and
                       any mutation proposals resolved from the tool
                       results. Terminal: the folded reply is the
                       assistant message.
         DIRECT_REPLY  non-blank reply already present. Terminal: the
                       reply is the assistant message.
         CLARIFICATION no reply, non-blank clarification question.
                       Terminal: the question is the assistant message.
         EMPTY         no usable text. Raises ``chat.empty_turn`` unless
                       the turn proposed actions/facts (then no assistant
                       message is persisted).

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
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.actions import get_action_kind, registered_kinds
from assistant.ai import AIProvider, get_ai_provider
from assistant.ai.prompts import TURN_FOLD_SYSTEM, TURN_SYSTEM
from assistant.ai.schemas import (
    ActionProposal,
    AssistantTurn,
    FactProposal,
    ReadToolRequest,
)
from assistant.i18n import DEFAULT_LANGUAGE, LocalizableError, language_name
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
    "calendar": "today's and upcoming calendar items (with ids)",
    "reminders": "pending reminders (with ids)",
    "workouts": "recent workout logs",
    "files": "stored files (name, state, size)",
    "documents": "search the text of stored documents for relevant excerpts",
    "facts": "confirmed user facts",
}


class TurnState(StrEnum):
    """Terminal state of a turn after the single structured call.

    Exactly one state per turn; see the module docstring for the full
    lifecycle. States are terminal — the engine never branches back to
    another model call except the single TOOL_FOLD fold.
    """

    TOOL_FOLD = "tool_fold"
    DIRECT_REPLY = "direct_reply"
    CLARIFICATION = "clarification"
    EMPTY = "empty"


def _classify_turn(turn: AssistantTurn) -> TurnState:
    """Classify a typed turn into exactly one state (deterministic).

    Priority is fixed and total:

    1. ``TOOL_FOLD`` — the model asked for app data it lacks: it has
       ``data_requests`` and no non-blank ``reply``.
    2. ``DIRECT_REPLY`` — a non-blank ``reply`` is present (it wins over
       any ``data_requests``: a reply means the model answered from the
       context it already had; the data requests are ignored).
    3. ``CLARIFICATION`` — no reply, but a non-blank clarification.
    4. ``EMPTY`` — none of the above.
    """
    if turn.data_requests and not (turn.reply and turn.reply.strip()):
        return TurnState.TOOL_FOLD
    if turn.reply and turn.reply.strip():
        return TurnState.DIRECT_REPLY
    if turn.clarification and turn.clarification.strip():
        return TurnState.CLARIFICATION
    return TurnState.EMPTY


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


async def run_read_tool(
    session: AsyncSession,
    user: User,
    *,
    tool: str,
    query: str | None = None,
    limit: int = 5,
    provider: AIProvider | None = None,
) -> tuple[str, list[files_service.RetrievedChunk]]:
    """Run one bounded, read-only tool over the existing services.

    Returns ``(text, chunks)``: a compact text block (never raises for empty
    data — it renders "(no data)") that folds straight into a prompt, plus
    the retrieved chunks (non-empty only for the ``documents`` tool) so the
    caller can render deterministic citations (SPEC §6.3).
    """
    if tool == "calendar":
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
        # Conditional retrieval (SPEC §6): the lexical gate inside
        # retrieve_chunks means a no-overlap query costs zero embeddings.
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
        lines = await facts_service.confirmed_lines(session, user)
        return ("\n".join(f"- {line}" for line in lines) if lines else "(no data)", [])
    return ("(unknown tool)", [])


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
    :class:`TurnState` (``TOOL_FOLD`` / ``DIRECT_REPLY`` / ``CLARIFICATION``
    / ``EMPTY``); only ``TOOL_FOLD`` triggers the one bounded second model
    call, and the settled state is exposed on the returned
    :class:`TurnResult`.

    Persists the user message and the assistant reply. Raises
    :class:`LocalizableError` (``chat.empty_turn``) when the model produced
    no usable output, and re-raises ``AIProviderError`` so the caller can
    apply its fallback; on both failure paths nothing from Phase C is
    committed, leaving the caller's session clean for its own writes.
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
            schema=AssistantTurn,
        )
        # The fold is the last word: no further data requests are
        # possible (the schema is not offered), and any stray
        # data_requests it produced are ignored — the turn never loops.
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
        created = await facts_service.propose_if_absent(
            session,
            user,
            value=fact.value,
            category=fact.category,
            provenance="assistant",
        )
        if created is not None:
            proposed_facts.append(created)

    # EMPTY state (and a TOOL_FOLD fold that came back blank with no
    # clarification) degrades safely: proposals/facts alone are a valid
    # turn, otherwise a localized empty-turn error (Phase C is aborted
    # before any write).
    final = reply if reply and reply.strip() else clarification
    if final is None:
        if proposed_facts or proposed:
            final = ""
        else:
            raise LocalizableError("chat.empty_turn")

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
