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
    ReadToolRequest,
)
from assistant.bot import callbacks
from assistant.bot.handlers import on_action
from assistant.i18n import LocalizableError
from assistant.models.calendar_items import CalendarItem, ItemKind
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.models.files import EMBEDDING_DIMENSIONS
from assistant.models.pending_actions import ActionStatus, PendingAction
from assistant.models.users import User
from assistant.services import actions as actions_service
from assistant.services import calendar as calendar_service
from assistant.services import turns as turns_service
from assistant.services import workouts as workouts_service
from assistant.services.users import upsert_user


async def _user(session: AsyncSession, user_id: int = 71) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="F")
    user.settings.language = "en"
    await session.commit()
    return user


class _FakeProvider:
    """Returns a fixed structured turn; the plain call returns ``reply``."""

    def __init__(
        self,
        turn: AssistantTurn,
        reply: str = "folded-reply",
    ) -> None:
        self.turn = turn
        self.reply = reply
        self.system: str | None = None
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
        self.system = system
        self.structured_calls += 1
        return self.turn

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
    assert provider.structured_calls == 1
    assert provider.chat_calls == 1
    # The tool result (with the item id) was folded into the final prompt.
    assert f"id={item.id}" in provider.chat_system
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
    # Two unique tools -> both rendered into the final system prompt.
    assert "[calendar]" in provider.chat_system
    assert "[workouts]" in provider.chat_system
    assert result.model_calls == 2


async def test_read_tools_are_user_scoped(session: AsyncSession) -> None:
    user = await _user(session, user_id=71)
    other = await _user(session, user_id=72)
    await calendar_service.create_item(
        session, other, title="Secret meeting", kind=ItemKind.event
    )
    await session.commit()

    out = await turns_service.run_read_tool(
        session, user, tool="calendar", limit=10
    )
    assert "Secret meeting" not in out


async def test_read_tool_unknown_returns_placeholder(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    out = await turns_service.run_read_tool(session, user, tool="nope")
    assert out == "(unknown tool)"


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
