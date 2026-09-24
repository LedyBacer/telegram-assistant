"""Bounded conversational action engine tests (SPEC §2, §3, §11).

Real PostgreSQL, fake provider. Covers the typed turn protocol, the bounded
read-tool layer, safe handling of malformed/empty model output, proposal of
durable pending actions, and the Telegram confirm/cancel wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProviderError
from assistant.ai.schemas import (
    ActionProposal,
    AssistantTurn,
    FactProposal,
    ReadToolRequest,
)
from assistant.bot import callbacks
from assistant.bot.handlers import on_action
from assistant.i18n import LocalizableError
from assistant.models.calendar_items import CalendarItem, ItemKind
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.models.facts import FactStatus, UserFact
from assistant.models.files import EMBEDDING_DIMENSIONS
from assistant.models.pending_actions import ActionStatus, PendingAction
from assistant.models.users import User
from assistant.services import actions as actions_service
from assistant.services import calendar as calendar_service
from assistant.services import facts as facts_service
from assistant.services import files as files_service
from assistant.services import reminders as reminders_service
from assistant.services import turns as turns_service
from assistant.services import workouts as workouts_service
from assistant.services.turns import TurnState
from assistant.services.users import upsert_user
from test_files import _FakeEmbedder, _indexed_file


async def _user(session: AsyncSession, user_id: int = 71) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="F")
    user.settings.language = "en"
    await session.commit()
    return user


class _FakeProvider:
    """Returns a fixed structured turn; the fold (second structured) call
    returns ``fold_turn`` (default: a plain reply ``reply``)."""

    def __init__(
        self,
        turn: AssistantTurn,
        reply: str = "folded-reply",
        fold_turn: AssistantTurn | None = None,
    ) -> None:
        self.turn = turn
        self.reply = reply
        self.fold_turn = fold_turn
        self.system: str | None = None
        self.fold_system: str | None = None
        self.chat_system: str | None = None
        self.structured_calls = 0
        self.chat_calls = 0
        self.embed_calls = 0

    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        self.chat_system = system
        self.chat_calls += 1
        return self.reply

    async def chat_structured(
        self, *, system: str, messages: list[dict[str, str]], schema
    ):
        if self.structured_calls == 0:
            self.system = system
        else:
            self.fold_system = system
        self.structured_calls += 1
        if self.structured_calls == 1:
            return self.turn
        if self.fold_turn is not None:
            return self.fold_turn
        return AssistantTurn(reply=self.reply)

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        self.embed_calls += 1
        return [0.0] * EMBEDDING_DIMENSIONS


class _FailingProvider:
    def __init__(self) -> None:
        self.structured_calls = 0
        self.embed_calls = 0

    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        raise AIProviderError("down")

    async def chat_structured(
        self, *, system: str, messages: list[dict[str, str]], schema
    ):
        self.structured_calls += 1
        raise AIProviderError("structured down")

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        self.embed_calls += 1
        return [0.0] * EMBEDDING_DIMENSIONS


async def _seed(session: AsyncSession, user: User) -> CalendarItem:
    # Noon UTC today is always inside the "today" window (avoids midnight
    # drift), so the item appears in list_today / the calendar read tool.
    today = datetime.now(tz=UTC).date()
    item = await calendar_service.create_item(
        session,
        user,
        title="Standup",
        kind=ItemKind.event,
        starts_at=datetime.combine(today, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=12),
    )
    await workouts_service.log_workout(session, user, name="Push", duration_minutes=30)
    await session.commit()
    return item


# ---------------------------------------------------------------------------
# Engine: direct reply
# ---------------------------------------------------------------------------


async def test_direct_reply_persists_both_messages(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    await _seed(session, user)
    provider = _FakeProvider(AssistantTurn(reply="You have standup."))

    result = await turns_service.run_turn(
        session, user, "what do I have today?", provider=provider
    )
    await session.commit()

    assert result.reply == "You have standup."
    assert result.proposed_actions == []
    assert result.model_calls == 1
    assert provider.structured_calls == 1
    assert provider.chat_calls == 0
    roles = [m.role for m in (await session.scalars(select(ChatMessage))).all()]
    assert roles == [ChatRole.user.value, ChatRole.assistant.value]


async def test_direct_reply_context_includes_item_ids(session: AsyncSession) -> None:
    user = await _user(session)
    item = await _seed(session, user)
    provider = _FakeProvider(AssistantTurn(reply="ok"))
    await turns_service.run_turn(session, user, "move standup", provider=provider)
    await session.commit()
    # The id is rendered so follow-up references can resolve (SPEC §3).
    assert f"id={item.id}" in provider.system


# ---------------------------------------------------------------------------
# Engine: proposed mutations
# ---------------------------------------------------------------------------


async def test_proposal_creates_pending_action_not_executed(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    await _seed(session, user)
    turn = AssistantTurn(
        reply="Shall I add that?",
        actions=[
            ActionProposal(
                kind="create_item",
                payload={"title": "Call Alex", "starts_at": "2026-09-24 18:30"},
                summary="Create task: Call Alex",
            )
        ],
    )
    provider = _FakeProvider(turn)

    result = await turns_service.run_turn(
        session, user, "remind me to call alex tomorrow at 18:30", provider=provider
    )
    await session.commit()

    assert len(result.proposed_actions) == 1
    action = result.proposed_actions[0]
    assert action.status == ActionStatus.proposed.value
    assert action.payload["title"] == "Call Alex"
    # A proposal must NOT create the calendar item itself.
    items = (await session.scalars(select(CalendarItem))).all()
    assert [i.title for i in items] == ["Standup"]


async def test_invalid_payload_is_skipped_not_stored(session: AsyncSession) -> None:
    user = await _user(session)
    turn = AssistantTurn(
        reply="Sure.",
        actions=[
            ActionProposal(
                kind="create_item",
                payload={"title": ""},  # violates min_length
                summary="Create task",
            )
        ],
    )
    provider = _FakeProvider(turn)

    result = await turns_service.run_turn(
        session, user, "add a task", provider=provider
    )
    await session.commit()

    assert result.proposed_actions == []
    assert len(result.skipped_actions) == 1
    assert (await session.scalars(select(PendingAction))).all() == []


async def test_unknown_kind_is_skipped(session: AsyncSession) -> None:
    user = await _user(session)
    turn = AssistantTurn(
        reply="Sure.",
        actions=[
            ActionProposal(
                kind="launch_rocket", payload={}, summary="Do a thing"
            )
        ],
    )
    provider = _FakeProvider(turn)
    result = await turns_service.run_turn(
        session, user, "do a thing", provider=provider
    )
    await session.commit()
    assert result.proposed_actions == []
    assert len(result.skipped_actions) == 1


# ---------------------------------------------------------------------------
# Engine: bounded read tools
# ---------------------------------------------------------------------------


async def test_data_request_makes_second_model_call(session: AsyncSession) -> None:
    user = await _user(session)
    item = await _seed(session, user)
    turn = AssistantTurn(
        data_requests=[ReadToolRequest(tool="calendar", limit=5)]
    )
    provider = _FakeProvider(turn, reply="Standup is your only item.")

    result = await turns_service.run_turn(
        session, user, "what is on my calendar?", provider=provider
    )
    await session.commit()

    assert result.model_calls == 2
    assert provider.structured_calls == 2
    assert provider.chat_calls == 0
    # The tool result (with the item id) was folded into the final prompt.
    assert f"id={item.id}" in provider.fold_system
    assert result.reply == "Standup is your only item."


async def test_data_request_dedupes_and_bounds_tools(session: AsyncSession) -> None:
    user = await _user(session)
    await _seed(session, user)
    # Duplicate calendar requests + an unknown tool + one valid -> bounded.
    turn = AssistantTurn(
        data_requests=[
            ReadToolRequest(tool="calendar"),
            ReadToolRequest(tool="calendar"),
            ReadToolRequest(tool="workouts"),
        ]
    )
    provider = _FakeProvider(turn, reply="done")
    result = await turns_service.run_turn(
        session, user, "list things", provider=provider
    )
    await session.commit()
    # Two unique tools -> both rendered into the final fold prompt.
    assert "[calendar]" in provider.fold_system
    assert "[workouts]" in provider.fold_system
    assert result.model_calls == 2


async def test_read_tools_are_user_scoped(session: AsyncSession) -> None:
    user = await _user(session, user_id=71)
    other = await _user(session, user_id=72)
    await calendar_service.create_item(
        session, other, title="Secret meeting", kind=ItemKind.event
    )
    await session.commit()

    out, chunks = await turns_service.run_read_tool(
        session, user, tool="calendar", limit=10
    )
    assert "Secret meeting" not in out
    assert chunks == []


async def test_read_tool_unknown_returns_placeholder(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    out, chunks = await turns_service.run_read_tool(session, user, tool="nope")
    assert out == "(unknown tool)"
    assert chunks == []


# ---------------------------------------------------------------------------
# Engine: deterministic entity resolution (P10)
# ---------------------------------------------------------------------------


async def _reminder(
    session: AsyncSession, user: User, message: str, offset_minutes: int = 60
):
    reminder = await reminders_service.create_reminder(
        session,
        user,
        fire_at=datetime.now(tz=UTC) + timedelta(minutes=offset_minutes),
        message=message,
    )
    await session.commit()
    return reminder


async def test_resolve_item_exact_and_substring(session: AsyncSession) -> None:
    user = await _user(session)
    item = await calendar_service.create_item(
        session, user, title="Standup", kind=ItemKind.event
    )
    await calendar_service.create_item(session, user, title="Lunch break")
    await session.commit()

    best, candidates = await calendar_service.resolve_item(session, user, "standup")
    assert best is not None and best.id == item.id
    assert [c.id for c in candidates] == [item.id]

    # Substring match also resolves uniquely.
    best, _ = await calendar_service.resolve_item(session, user, "stand")
    assert best is not None and best.id == item.id

    # Empty / no-match queries never invent an entity.
    assert await calendar_service.resolve_item(session, user, "  ") == (None, [])
    assert await calendar_service.resolve_item(session, user, "nope") == (None, [])


async def test_resolve_item_ambiguous_lists_candidates(session: AsyncSession) -> None:
    user = await _user(session)
    await calendar_service.create_item(session, user, title="Gym session")
    await calendar_service.create_item(session, user, title="Gym morning")
    await session.commit()

    best, candidates = await calendar_service.resolve_item(session, user, "gym")
    assert best is None
    assert [c.title for c in candidates] == ["Gym session", "Gym morning"]


async def test_resolve_item_ignores_completed(session: AsyncSession) -> None:
    user = await _user(session)
    item = await calendar_service.create_item(session, user, title="Done task")
    await calendar_service.complete_item(session, user, item.id)
    await session.commit()

    best, candidates = await calendar_service.resolve_item(session, user, "done")
    assert best is None and candidates == []


async def test_resolve_reminder_unique_and_ambiguous(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    await _reminder(session, user, "Call dentist", 60)
    bank = await _reminder(session, user, "Call bank", 90)
    await _reminder(session, user, "Call dentist again", 120)

    best, candidates = await reminders_service.resolve_reminder(
        session, user, "call bank"
    )
    assert best is not None and best.id == bank.id

    best, candidates = await reminders_service.resolve_reminder(
        session, user, "dentist"
    )
    assert best is None
    assert [c.message for c in candidates] == [
        "Call dentist",
        "Call dentist again",
    ]
    assert await reminders_service.resolve_reminder(session, user, "nope") == (
        None,
        [],
    )


async def test_read_tools_render_resolution_line(session: AsyncSession) -> None:
    user = await _user(session)
    item = await calendar_service.create_item(
        session, user, title="Standup", kind=ItemKind.event
    )
    gym_session = await calendar_service.create_item(
        session, user, title="Gym session"
    )
    gym_morning = await calendar_service.create_item(
        session, user, title="Gym morning"
    )
    reminder = await _reminder(session, user, "Water plants")
    await session.commit()

    out, _ = await turns_service.run_read_tool(
        session, user, tool="calendar", query="standup"
    )
    first = out.splitlines()[0]
    assert first.startswith(f"match: id={item.id} Standup")

    out, _ = await turns_service.run_read_tool(
        session, user, tool="calendar", query="gym"
    )
    assert out.splitlines()[0] == (
        f"ambiguous: id={gym_session.id} Gym session; "
        f"id={gym_morning.id} Gym morning"
    )

    out, _ = await turns_service.run_read_tool(
        session, user, tool="calendar", query="nope"
    )
    assert out.splitlines()[0] == "match: none"

    out, _ = await turns_service.run_read_tool(
        session, user, tool="reminders", query="water"
    )
    assert out.splitlines()[0].startswith(f"match: id={reminder.id} Water plants")


# ---------------------------------------------------------------------------
# Engine: clarification + safe failure
# ---------------------------------------------------------------------------


async def test_clarification_used_when_no_reply(session: AsyncSession) -> None:
    user = await _user(session)
    provider = _FakeProvider(
        AssistantTurn(clarification="Which item do you mean?")
    )
    result = await turns_service.run_turn(
        session, user, "move it to 20:00", provider=provider
    )
    await session.commit()
    assert result.reply == "Which item do you mean?"
    roles = [m.role for m in (await session.scalars(select(ChatMessage))).all()]
    assert roles == [ChatRole.user.value, ChatRole.assistant.value]


async def test_empty_turn_raises_localizable(session: AsyncSession) -> None:
    user = await _user(session)
    provider = _FakeProvider(AssistantTurn())
    with pytest.raises(LocalizableError) as exc:
        await turns_service.run_turn(session, user, "hello", provider=provider)
    assert exc.value.key == "chat.empty_turn"
    await session.commit()
    # Nothing persisted for a turn that produced no output.
    assert (await session.scalars(select(ChatMessage))).all() == []


async def test_blank_text_rejected(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ValueError, match="required"):
        await turns_service.run_turn(
            session, user, "   ", provider=_FakeProvider(AssistantTurn(reply="x"))
        )


async def test_provider_failure_re_raises(session: AsyncSession) -> None:
    user = await _user(session)
    provider = _FailingProvider()
    with pytest.raises(AIProviderError, match="structured down"):
        await turns_service.run_turn(session, user, "hello", provider=provider)
    await session.commit()
    assert (await session.scalars(select(ChatMessage))).all() == []


# ---------------------------------------------------------------------------
# Engine: formal turn state machine
# ---------------------------------------------------------------------------


async def test_state_direct_reply(session: AsyncSession) -> None:
    user = await _user(session)
    provider = _FakeProvider(AssistantTurn(reply="ok"))
    result = await turns_service.run_turn(
        session, user, "hi", provider=provider
    )
    await session.commit()
    assert result.state is TurnState.DIRECT_REPLY
    assert result.model_calls == 1
    assert provider.structured_calls == 1
    assert provider.chat_calls == 0


async def test_state_tool_fold(session: AsyncSession) -> None:
    user = await _user(session)
    await _seed(session, user)
    turn = AssistantTurn(data_requests=[ReadToolRequest(tool="calendar")])
    provider = _FakeProvider(turn, reply="folded")
    result = await turns_service.run_turn(
        session, user, "what's on my calendar?", provider=provider
    )
    await session.commit()
    assert result.state is TurnState.TOOL_FOLD
    assert result.model_calls == 2
    assert provider.structured_calls == 2
    assert provider.chat_calls == 0
    assert result.reply == "folded"


async def test_state_clarification(session: AsyncSession) -> None:
    user = await _user(session)
    provider = _FakeProvider(AssistantTurn(clarification="Which one?"))
    result = await turns_service.run_turn(
        session, user, "move it", provider=provider
    )
    await session.commit()
    assert result.state is TurnState.CLARIFICATION
    assert result.model_calls == 1
    assert result.reply == "Which one?"


async def test_state_empty_with_proposal_succeeds(session: AsyncSession) -> None:
    user = await _user(session)
    turn = AssistantTurn(
        actions=[
            ActionProposal(
                kind="create_item",
                payload={"title": "Gym", "kind": "task"},
                summary="Create task: Gym",
            )
        ]
    )
    result = await turns_service.run_turn(
        session, user, "gym tomorrow", provider=_FakeProvider(turn)
    )
    await session.commit()
    assert result.state is TurnState.EMPTY
    assert result.reply == ""
    assert len(result.proposed_actions) == 1
    # No assistant message is persisted for a proposals-only turn.
    roles = [m.role for m in (await session.scalars(select(ChatMessage))).all()]
    assert roles == [ChatRole.user.value]


async def test_classification_priority() -> None:
    # Fixed, total priority: tool fold > reply > clarification > empty.
    assert (
        turns_service._classify_turn(
            AssistantTurn(
                reply="answered", data_requests=[ReadToolRequest(tool="facts")]
            )
        )
        is TurnState.DIRECT_REPLY
    )
    assert (
        turns_service._classify_turn(
            AssistantTurn(
                reply="", data_requests=[ReadToolRequest(tool="facts")]
            )
        )
        is TurnState.TOOL_FOLD
    )
    # A blank (whitespace-only) reply counts as no reply -> the model's
    # data requests are honoured with the one fold call.
    assert (
        turns_service._classify_turn(
            AssistantTurn(
                reply="   ", data_requests=[ReadToolRequest(tool="facts")]
            )
        )
        is TurnState.TOOL_FOLD
    )
    assert (
        turns_service._classify_turn(
            AssistantTurn(clarification="Which?")
        )
        is TurnState.CLARIFICATION
    )
    assert (
        turns_service._classify_turn(AssistantTurn(clarification="  "))
        is TurnState.EMPTY
    )
    assert turns_service._classify_turn(AssistantTurn()) is TurnState.EMPTY


async def test_reply_wins_over_data_requests_end_to_end(
    session: AsyncSession,
) -> None:
    """DIRECT_REPLY is terminal: a reply ignores any data_requests — no
    read tools run and no second model call is made."""
    user = await _user(session)
    await _seed(session, user)
    turn = AssistantTurn(
        reply="I already know your calendar.",
        data_requests=[ReadToolRequest(tool="calendar")],
    )
    provider = _FakeProvider(turn)
    result = await turns_service.run_turn(
        session, user, "calendar?", provider=provider
    )
    await session.commit()
    assert result.state is TurnState.DIRECT_REPLY
    assert result.model_calls == 1
    assert provider.structured_calls == 1
    assert provider.chat_calls == 0
    assert result.reply == "I already know your calendar."
    assert result.retrieved_chunks == []


async def test_tool_fold_blank_fold_without_clarification_raises(
    session: AsyncSession,
) -> None:
    """TOOL_FOLD whose fold comes back blank (and no clarification)
    degrades to the EMPTY failure path: nothing is persisted."""
    user = await _user(session)
    turn = AssistantTurn(data_requests=[ReadToolRequest(tool="facts")])
    provider = _FakeProvider(turn, reply="   ")
    with pytest.raises(LocalizableError) as exc:
        await turns_service.run_turn(session, user, "what do you know?", provider=provider)
    assert exc.value.key == "chat.empty_turn"
    await session.commit()
    assert provider.structured_calls == 2
    assert provider.chat_calls == 0
    assert (await session.scalars(select(ChatMessage))).all() == []


# ---------------------------------------------------------------------------
# Engine: lookup→mutation via the structured fold
# ---------------------------------------------------------------------------


async def test_lookup_then_mutation_in_two_calls(
    session: AsyncSession,
) -> None:
    """Call 1 asks for the data the mutation depends on; the fold (call 2)
    proposes the mutation with the real id revealed by the tool results."""
    user = await _user(session)
    item = await _seed(session, user)
    turn1 = AssistantTurn(data_requests=[ReadToolRequest(tool="calendar")])
    fold = AssistantTurn(
        reply="Shall I move Standup to 20:00?",
        actions=[
            ActionProposal(
                kind="update_item",
                payload={"item_id": item.id, "starts_at": "2026-09-24 20:00"},
                summary="Move Standup to 20:00",
            )
        ],
    )
    provider = _FakeProvider(turn1, fold_turn=fold)

    result = await turns_service.run_turn(
        session, user, "move standup to 20:00", provider=provider
    )
    await session.commit()

    assert result.state is TurnState.TOOL_FOLD
    assert result.model_calls == 2
    assert result.reply == "Shall I move Standup to 20:00?"
    # The tool result (with the item id) was in the fold prompt.
    assert f"id={item.id}" in provider.fold_system
    assert len(result.proposed_actions) == 1
    action = result.proposed_actions[0]
    assert action.status == ActionStatus.proposed.value
    assert action.payload["item_id"] == item.id
    # The optimistic baseline is captured at proposal time.
    assert action.payload.get("expected_updated_at")
    # The proposal is durable; the item itself is untouched until confirm.
    stored = await session.get(PendingAction, action.id)
    assert stored.status == ActionStatus.proposed.value
    refreshed = await session.get(CalendarItem, item.id)
    assert refreshed.status == "scheduled"


async def test_fold_data_requests_are_ignored(session: AsyncSession) -> None:
    """The fold is the last call: stray data_requests in it cannot start a
    third call (no ReAct loop)."""
    user = await _user(session)
    turn1 = AssistantTurn(data_requests=[ReadToolRequest(tool="facts")])
    fold = AssistantTurn(
        reply="ok",
        data_requests=[ReadToolRequest(tool="calendar")],
    )
    provider = _FakeProvider(turn1, fold_turn=fold)

    result = await turns_service.run_turn(
        session, user, "what do you know?", provider=provider
    )
    await session.commit()

    assert result.state is TurnState.TOOL_FOLD
    assert result.model_calls == 2
    assert provider.structured_calls == 2
    assert result.reply == "ok"


async def test_fold_clarification_used_when_reply_blank(
    session: AsyncSession,
) -> None:
    """When the tool results leave the request ambiguous, the fold's
    clarification becomes the assistant message."""
    user = await _user(session)
    turn1 = AssistantTurn(data_requests=[ReadToolRequest(tool="calendar")])
    fold = AssistantTurn(clarification="Which standup do you mean?")
    provider = _FakeProvider(turn1, fold_turn=fold)

    result = await turns_service.run_turn(
        session, user, "move standup", provider=provider
    )
    await session.commit()

    assert result.state is TurnState.TOOL_FOLD
    assert result.reply == "Which standup do you mean?"
    roles = [m.role for m in (await session.scalars(select(ChatMessage))).all()]
    assert roles == [ChatRole.user.value, ChatRole.assistant.value]


# ---------------------------------------------------------------------------
# Telegram confirm / cancel wiring
# ---------------------------------------------------------------------------


def _fake_callback(action_id: int, action: str) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=SimpleNamespace(
            id=71, first_name="F", last_name=None, username=None, is_bot=False
        ),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


async def test_on_action_confirm_executes(session: AsyncSession) -> None:
    user = await _user(session)
    action = await actions_service.propose_action(
        session,
        user,
        kind="create_item",
        payload={"title": "Gym", "kind": "task"},
        summary="Create task: Gym",
    )
    await session.commit()

    callback = _fake_callback(action.id, "confirm")
    await on_action(
        callback,
        callbacks.ActionCallback(action="confirm", action_id=action.id),
        session,
    )
    await session.commit()

    stored = await session.get(PendingAction, action.id)
    assert stored.status == ActionStatus.executed.value
    items = (await session.scalars(select(CalendarItem))).all()
    assert [i.title for i in items] == ["Gym"]
    assert "Done:" in callback.message.edit_text.await_args.args[0]


async def test_on_action_cancel_rejects(session: AsyncSession) -> None:
    user = await _user(session)
    action = await actions_service.propose_action(
        session,
        user,
        kind="create_item",
        payload={"title": "Gym", "kind": "task"},
        summary="Create task: Gym",
    )
    await session.commit()

    callback = _fake_callback(action.id, "cancel")
    await on_action(
        callback,
        callbacks.ActionCallback(action="cancel", action_id=action.id),
        session,
    )
    await session.commit()

    stored = await session.get(PendingAction, action.id)
    assert stored.status == ActionStatus.rejected.value
    # Cancelled action must not create the item.
    assert (await session.scalars(select(CalendarItem))).all() == []


async def test_on_action_confirm_stale_expires(session: AsyncSession) -> None:
    user = await _user(session)
    item = await calendar_service.create_item(
        session, user, title="Standup", kind=ItemKind.event
    )
    # Delete the target so the complete_item action becomes stale.
    action = await actions_service.propose_action(
        session,
        user,
        kind="complete_item",
        payload={"item_id": item.id},
        summary="Complete: Standup",
    )
    await calendar_service.delete_item(session, user, item.id)
    await session.commit()

    callback = _fake_callback(action.id, "confirm")
    await on_action(
        callback,
        callbacks.ActionCallback(action="confirm", action_id=action.id),
        session,
    )
    await session.commit()

    stored = await session.get(PendingAction, action.id)
    assert stored.status == ActionStatus.expired.value
    # The localized "expired" message is shown, not an exception.
    assert "no longer available" in callback.message.edit_text.await_args.args[0]


async def test_on_action_other_user_cannot_touch(session: AsyncSession) -> None:
    owner = await _user(session, user_id=71)
    action = await actions_service.propose_action(
        session,
        owner,
        kind="create_item",
        payload={"title": "Gym", "kind": "task"},
        summary="Create task: Gym",
    )
    await session.commit()

    # A different user pressing the callback cannot execute it.
    callback = SimpleNamespace(
        from_user=SimpleNamespace(
            id=999, first_name="X", last_name=None, username=None, is_bot=False
        ),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await on_action(
        callback,
        callbacks.ActionCallback(action="confirm", action_id=action.id),
        session,
    )
    await session.commit()

    stored = await session.get(PendingAction, action.id)
    assert stored.status == ActionStatus.proposed.value
    assert (await session.scalars(select(CalendarItem))).all() == []
    # The (RU, default-locale) "unavailable" message is shown to the foreign
    # user, not an exception.
    assert "недоступно" in callback.message.edit_text.await_args.args[0]


# ---------------------------------------------------------------------------
# Automatic memory proposals (SPEC §14)
# ---------------------------------------------------------------------------


async def test_fact_proposal_creates_proposed_not_confirmed(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    turn = AssistantTurn(
        reply="Noted.",
        facts=[FactProposal(value="I prefer tea over coffee")],
    )
    result = await turns_service.run_turn(
        session, user, "by the way I prefer tea over coffee", provider=_FakeProvider(turn)
    )
    await session.commit()

    assert len(result.proposed_facts) == 1
    fact = result.proposed_facts[0]
    assert fact.status == FactStatus.proposed.value
    assert fact.value == "I prefer tea over coffee"
    # It is NOT used as trusted context until confirmed.
    assert await facts_service.confirmed_lines(session, user) == []


async def test_fact_dedupes_when_already_live(session: AsyncSession) -> None:
    user = await _user(session)
    turn = AssistantTurn(reply="ok", facts=[FactProposal(value="I am an engineer")])
    provider = _FakeProvider(turn)

    first = await turns_service.run_turn(
        session, user, "remember I am an engineer", provider=provider
    )
    await session.commit()
    assert len(first.proposed_facts) == 1

    # Same fact again -> deduped, no duplicate row created.
    second = await turns_service.run_turn(
        session, user, "I am an engineer, remember", provider=provider
    )
    await session.commit()
    assert second.proposed_facts == []
    assert len((await session.scalars(select(UserFact))).all()) == 1


async def test_fact_dedupes_rejected(session: AsyncSession) -> None:
    user = await _user(session)
    existing = await facts_service.propose_fact(
        session, user, value="I live in Berlin"
    )
    await facts_service.reject_fact(session, user, existing.id)
    await session.commit()

    # A rejected fact must not be re-proposed.
    result = await turns_service.run_turn(
        session,
        user,
        "I live in Berlin",
        provider=_FakeProvider(
            AssistantTurn(reply="ok", facts=[FactProposal(value="I live in Berlin")])
        ),
    )
    await session.commit()
    assert result.proposed_facts == []
    assert len((await session.scalars(select(UserFact))).all()) == 1


async def test_fact_superseded_allows_reproposal(session: AsyncSession) -> None:
    user = await _user(session)
    old = await facts_service.propose_fact(
        session, user, value="I drink tea"
    )
    replaced = await facts_service.supersede_fact(
        session, user, old.id, value="I drink green tea"
    )
    assert replaced is not None
    # Force the old "I drink tea" key to a superseded state so a fresh
    # proposal on the same key is allowed again.
    old.status = FactStatus.superseded.value
    await session.commit()

    result = await turns_service.run_turn(
        session,
        user,
        "update: I drink tea",
        provider=_FakeProvider(
            AssistantTurn(reply="ok", facts=[FactProposal(value="I drink tea")])
        ),
    )
    await session.commit()
    assert len(result.proposed_facts) == 1


async def test_facts_only_turn_does_not_raise(session: AsyncSession) -> None:
    user = await _user(session)
    turn = AssistantTurn(facts=[FactProposal(value="I like running")])
    result = await turns_service.run_turn(
        session, user, "I like running", provider=_FakeProvider(turn)
    )
    await session.commit()
    # No reply text, but a fact was proposed -> no empty-turn error.
    assert result.reply == ""
    assert len(result.proposed_facts) == 1


# ---------------------------------------------------------------------------
# Conditional document retrieval (SPEC §6)
# ---------------------------------------------------------------------------


async def test_ordinary_turn_makes_no_embedding_calls(
    session: AsyncSession,
) -> None:
    """Ordinary chat must not invoke the embedding provider (SPEC §6)."""
    user = await _user(session)
    # A stored document exists; the greeting shares no words with it.
    await _indexed_file(
        session, user, filename="quantum-notes.md",
        texts=["quantum entanglement basics"],
    )
    provider = _FakeProvider(AssistantTurn(reply="Hello!"))

    result = await turns_service.run_turn(
        session, user, "hello", provider=provider
    )
    await session.commit()

    assert result.reply == "Hello!"
    assert provider.embed_calls == 0
    assert result.retrieved_chunks == []


async def test_documents_tool_retrieves_with_deterministic_citations(
    session: AsyncSession,
) -> None:
    """The documents read tool triggers retrieval; citations come from
    retrieval metadata, not the model (SPEC §6.3)."""
    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="warranty.txt",
        texts=["the warranty covers repairs for two years"],
    )
    turn = AssistantTurn(
        data_requests=[
            ReadToolRequest(tool="documents", query="warranty repairs", limit=5)
        ]
    )
    provider = _FakeProvider(
        turn, reply="Your warranty covers repairs for two years."
    )

    result = await turns_service.run_turn(
        session,
        user,
        "what does my warranty document say about repairs?",
        provider=provider,
    )
    await session.commit()

    assert result.reply == "Your warranty covers repairs for two years."
    assert provider.structured_calls == 2
    assert provider.chat_calls == 0
    assert len(result.retrieved_chunks) == 1
    chunk = result.retrieved_chunks[0]
    assert chunk.file_name == "warranty.txt"
    assert "warranty" in provider.fold_system
    # Deterministic, application-rendered citation (SPEC §6.3).
    assert files_service.format_citations(result.retrieved_chunks) == "Sources: warranty.txt"


async def test_documents_tool_no_overlap_makes_no_embedding_call(
    session: AsyncSession,
) -> None:
    """The lexical gate: no word overlap -> no results, zero embeddings."""
    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="quantum-notes.md",
        texts=["quantum entanglement basics"],
    )
    out, chunks = await turns_service.run_read_tool(
        session, user, tool="documents", query="good morning"
    )
    assert out == "(no data)"
    assert chunks == []


async def test_documents_tool_survives_embedding_outage(session: AsyncSession) -> None:
    """An embedding outage degrades to lexical-only retrieval (SPEC §6)."""
    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="warranty.txt",
        texts=["the warranty covers repairs for two years"],
    )

    async def _down(**kwargs) -> list[float]:
        raise AIProviderError("embedding server down")

    embedder = _FakeEmbedder()
    embedder.embed_query = _down  # type: ignore[method-assign]
    out, chunks = await turns_service.run_read_tool(
        session,
        user,
        tool="documents",
        query="warranty repairs",
        limit=5,
        provider=embedder,
    )
    assert out != "(no data)"
    assert len(chunks) == 1
    assert chunks[0].file_name == "warranty.txt"


async def test_documents_tool_is_user_scoped(session: AsyncSession) -> None:
    user = await _user(session, user_id=71)
    other = await _user(session, user_id=72)
    await _indexed_file(
        session, other, filename="theirs.txt",
        texts=["secret warranty terms about repairs"],
    )
    out, chunks = await turns_service.run_read_tool(
        session, user, tool="documents", query="warranty repairs"
    )
    assert out == "(no data)"
    assert chunks == []


async def test_no_transaction_spans_provider_calls(session: AsyncSession) -> None:
    """Phase A/B/C: no DB transaction is open during the structured model
    call, the documents tool's embedding call, or the second model call."""
    user = await _user(session)
    await _indexed_file(
        session, user, filename="warranty.txt",
        texts=["the warranty covers repairs for two years"],
    )
    provider = _FakeProvider(
        AssistantTurn(
            data_requests=[
                ReadToolRequest(tool="documents", query="warranty repairs")
            ]
        ),
        reply="folded",
    )
    observed: dict[str, bool] = {}

    orig_structured = provider.chat_structured
    orig_embed = provider.embed_query

    async def structured(*, system, messages, schema):
        key = "structured" if provider.structured_calls == 0 else "fold"
        observed[key] = session.in_transaction()
        return await orig_structured(
            system=system, messages=messages, schema=schema
        )

    async def embed(**kwargs):
        observed["embed"] = session.in_transaction()
        return await orig_embed(**kwargs)

    provider.chat_structured = structured
    provider.embed_query = embed

    result = await turns_service.run_turn(
        session, user, "what about warranty repairs?", provider=provider
    )
    await session.commit()

    assert observed == {
        "structured": False,
        "embed": False,
        "fold": False,
    }
    assert result.model_calls == 2
    assert len(result.retrieved_chunks) == 1


# ---------------------------------------------------------------------------
# V3 P41: realistic malformed/ambiguous Qwen fixtures at the engine level
# ---------------------------------------------------------------------------


async def test_fixture_invalid_enum_payload_skipped_not_stored(
    session: AsyncSession,
) -> None:
    """A 9B model may emit an enum value outside the schema (e.g.
    priority "urgent"); the engine must skip the proposal, store nothing,
    and still deliver the reply."""
    user = await _user(session)
    turn = AssistantTurn(
        reply="Sure, I'll set it as urgent.",
        actions=[
            ActionProposal(
                kind="create_item",
                payload={"title": "Gym", "priority": "urgent"},
                summary="Create task: Gym (urgent)",
            )
        ],
    )
    result = await turns_service.run_turn(
        session, user, "gym, urgent", provider=_FakeProvider(turn)
    )
    await session.commit()
    assert result.reply == "Sure, I'll set it as urgent."
    assert result.proposed_actions == []
    assert len(result.skipped_actions) == 1
    assert (await session.scalars(select(PendingAction))).all() == []


async def test_fixture_ambiguous_reference_yields_clarification_no_mutation(
    session: AsyncSession,
) -> None:
    """Two similar items: the read tool reports 'ambiguous: ...' and the
    fold asks for clarification instead of guessing — no mutation is
    proposed (V3 §8)."""
    user = await _user(session)
    at = datetime.combine(datetime.now(tz=UTC).date(), datetime.min.time(), tzinfo=UTC)
    at = at + timedelta(hours=12)
    await calendar_service.create_item(
        session, user, title="Dentist", starts_at=at
    )
    await calendar_service.create_item(
        session, user, title="Dentist (checkup)", starts_at=at
    )
    await session.commit()

    turn = AssistantTurn(
        data_requests=[ReadToolRequest(tool="calendar", query="dentist")]
    )
    fold = AssistantTurn(clarification="Which dentist item — the visit or the checkup?")
    provider = _FakeProvider(turn, fold_turn=fold)
    result = await turns_service.run_turn(
        session, user, "cancel the dentist", provider=provider
    )
    await session.commit()
    assert result.state is TurnState.TOOL_FOLD
    assert result.reply == "Which dentist item — the visit or the checkup?"
    assert result.proposed_actions == []
    assert (await session.scalars(select(PendingAction))).all() == []


def test_actions_doc_lists_types_enums_and_dates() -> None:
    doc = turns_service._actions_doc()
    assert "priority:low|normal|high" in doc
    assert "item_id:int" in doc
    assert '"YYYY-MM-DD HH:MM"' in doc
    assert "remind_offsets_minutes:int[]?" in doc
    # Internal engine fields are never offered to the model.
    assert "expected_updated_at" not in doc


async def test_turn_prompts_carry_action_and_tool_docs(session: AsyncSession) -> None:
    user = await _user(session)
    provider = _FakeProvider(AssistantTurn(reply="ok"))
    await turns_service.run_turn(session, user, "hi", provider=provider)
    await session.commit()
    assert "- create_item(title:str" in provider.system
    assert "query=<the item the user named> resolves it" in provider.system
    assert "Each request: {tool, query" in provider.system
