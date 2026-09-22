"""Contextual AI chat tests (SPEC §15) — real PostgreSQL, fake provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProviderError
from assistant.bot.handlers import on_text
from assistant.bot.states import TaskDraftStates
from assistant.models.calendar_items import ItemKind
from assistant.models.chat_messages import ChatMessage, ChatRole
from assistant.models.files import EMBEDDING_DIMENSIONS
from assistant.models.users import User
from assistant.services import calendar as calendar_service
from assistant.services import chat as chat_service
from assistant.services import facts as facts_service
from assistant.services import reminders as reminders_service
from assistant.services import workouts as workouts_service
from assistant.services.users import upsert_user


async def _user(session: AsyncSession, user_id: int = 71) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="F")
    await session.commit()
    return user


class _FakeProvider:
    """Records the last prompt; returns a fixed reply."""

    def __init__(self, reply: str = "ok-reply") -> None:
        self.reply = reply
        self.system: str | None = None
        self.messages: list[dict[str, str]] | None = None
        self.embed_calls = 0

    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        self.system = system
        self.messages = list(messages)
        return self.reply

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        self.embed_calls += 1
        return [0.0] * EMBEDDING_DIMENSIONS


class _FailingProvider:
    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        raise AIProviderError("chat completion failed: down")

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        return [0.0] * EMBEDDING_DIMENSIONS


async def _seed_context(session: AsyncSession, user: User) -> None:
    await facts_service.propose_fact(session, user, value="loves coffee", provenance="test")
    proposed = await facts_service.list_facts(session, user, status=None)
    await facts_service.confirm_fact(session, user, proposed[0].id)
    # "Today at noon (UTC)" is always inside the current day's window,
    # unlike now+30min which drifts past the day boundary near midnight.
    today = datetime.now(tz=UTC).date()
    item = await calendar_service.create_item(
        session,
        user,
        title="Team standup",
        kind=ItemKind.event,
        starts_at=datetime.combine(today, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=12),
    )
    await reminders_service.create_reminder(
        session, user, fire_at=item.starts_at, message="Reminder: Team standup"
    )
    await workouts_service.log_workout(
        session, user, name="Push day", duration_minutes=45, perceived_effort=7
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


async def test_build_context_selects_relevant_sections(session: AsyncSession) -> None:
    user = await _user(session)
    await _seed_context(session, user)

    ctx = await chat_service.build_context(
        session, user, "what's coming up?", provider=_FakeProvider()
    )

    assert ctx.fact_lines == ["loves coffee"]
    assert [i.title for i in ctx.today_items] == ["Team standup"]
    assert any(r.message == "Reminder: Team standup" for r in ctx.reminders)
    assert [w.name for w in ctx.workouts] == ["Push day"]

    rendered = chat_service.render_context(ctx)
    assert "loves coffee" in rendered
    assert "Team standup" in rendered
    assert "Push day" in rendered
    assert "45 min" in rendered


async def test_build_context_excludes_other_users_data(session: AsyncSession) -> None:
    user = await _user(session, user_id=71)
    other = await _user(session, user_id=72)
    await facts_service.propose_fact(session, other, value="secret of other")
    other_facts = await facts_service.list_facts(session, other)
    await facts_service.confirm_fact(session, other, other_facts[0].id)
    await calendar_service.create_item(
        session, other, title="Other's meeting",
        starts_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await session.commit()

    ctx = await chat_service.build_context(session, user, "hi", provider=_FakeProvider())
    rendered = chat_service.render_context(ctx)
    assert "secret of other" not in rendered
    assert "Other's meeting" not in rendered
    assert ctx.fact_lines == []


async def test_render_context_empty_is_placeholder(session: AsyncSession) -> None:
    user = await _user(session)
    ctx = await chat_service.build_context(session, user, "hi", provider=_FakeProvider())
    assert chat_service.render_context(ctx) == "(no additional context)"


# ---------------------------------------------------------------------------
# Chat turn
# ---------------------------------------------------------------------------


async def test_chat_persists_both_messages_and_uses_context(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    await _seed_context(session, user)
    provider = _FakeProvider(reply="You have standup at noon.")

    reply = await chat_service.chat(session, user, "what do I have today?", provider=provider)
    await session.commit()

    assert reply == "You have standup at noon."
    messages = (
        await session.scalars(
            select(ChatMessage)
            .where(ChatMessage.user_id == user.id)
            .order_by(ChatMessage.id)
        )
    ).all()
    assert [(m.role, m.content) for m in messages] == [
        (ChatRole.user.value, "what do I have today?"),
        (ChatRole.assistant.value, "You have standup at noon."),
    ]
    assert "loves coffee" in provider.system
    assert "Team standup" in provider.system
    assert provider.messages[-1] == {"role": "user", "content": "what do I have today?"}


async def test_chat_replies_use_prior_history(session: AsyncSession) -> None:
    user = await _user(session)
    provider = _FakeProvider(reply="first")
    await chat_service.chat(session, user, "hello", provider=provider)
    await session.commit()

    provider2 = _FakeProvider(reply="second")
    await chat_service.chat(session, user, "and again", provider=provider2)
    await session.commit()

    assert {"role": "user", "content": "hello"} in provider2.messages
    assert {"role": "assistant", "content": "first"} in provider2.messages


async def test_chat_provider_failure_raises_and_persists_nothing(
    session: AsyncSession,
) -> None:
    user = await _user(session)
    with pytest.raises(AIProviderError, match="down"):
        await chat_service.chat(session, user, "hello", provider=_FailingProvider())
    await session.commit()

    assert (await session.scalars(select(ChatMessage))).all() == []


async def test_chat_rejects_blank_text(session: AsyncSession) -> None:
    user = await _user(session)
    with pytest.raises(ValueError, match="required"):
        await chat_service.chat(session, user, "   ", provider=_FakeProvider())


# ---------------------------------------------------------------------------
# Bot wiring
# ---------------------------------------------------------------------------


def _fake_text_message(text: str, user_id: int = 71) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(
            id=user_id, first_name="F", last_name=None, username=None, is_bot=False
        ),
        answer=AsyncMock(),
    )


def _fake_state() -> SimpleNamespace:
    return SimpleNamespace(
        get_state=AsyncMock(return_value=None),
        set_state=AsyncMock(),
        clear=AsyncMock(),
        update_data=AsyncMock(),
    )


async def test_on_text_uses_contextual_chat(session: AsyncSession, monkeypatch) -> None:
    monkeypatch.setattr(
        chat_service, "get_ai_provider", lambda: _FakeProvider(reply="chat-reply")
    )
    message = _fake_text_message("what do I have today?")
    await on_text(message, session, _fake_state())
    await session.commit()

    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == "chat-reply"
    roles = [m.role for m in (await session.scalars(select(ChatMessage))).all()]
    assert roles == [ChatRole.user.value, ChatRole.assistant.value]


async def test_on_text_provider_down_gives_fallback(session: AsyncSession, monkeypatch) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.commit()

    monkeypatch.setattr(
        chat_service, "get_ai_provider", lambda: _FailingProvider()
    )
    message = _fake_text_message("hello there")
    await on_text(message, session, _fake_state())
    await session.commit()

    message.answer.assert_awaited_once()
    assert "can't reach my language model" in message.answer.await_args.args[0]
    roles = [m.role for m in (await session.scalars(select(ChatMessage))).all()]
    assert roles == [ChatRole.user.value]


async def test_on_text_still_routes_fsm_states(session: AsyncSession, monkeypatch) -> None:
    user = await _user(session)
    user.settings.language = "en"
    await session.commit()

    monkeypatch.setattr(
        chat_service, "get_ai_provider", lambda: _FakeProvider(reply="chat-reply")
    )
    state = SimpleNamespace(
        get_state=AsyncMock(return_value=TaskDraftStates.confirm),
        set_state=AsyncMock(),
        clear=AsyncMock(),
        update_data=AsyncMock(),
    )
    message = _fake_text_message("confirm it")
    await on_text(message, session, state)
    await session.commit()

    message.answer.assert_awaited_once()
    assert "buttons" in message.answer.await_args.args[0]
    assert (await session.scalars(select(ChatMessage))).all() == []
