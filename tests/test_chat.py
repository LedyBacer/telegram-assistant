"""Contextual AI chat tests (SPEC §15) — real PostgreSQL, fake provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import AIProviderError
from assistant.ai.schemas import AITaskDraft, AssistantTurn
from assistant.bot import handlers
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
from assistant.services import turns as turns_service
from assistant.services import workouts as workouts_service
from assistant.services.users import upsert_user


async def _user(session: AsyncSession, user_id: int = 71) -> User:
    user, _ = await upsert_user(session, user_id=user_id, first_name="F")
    await session.commit()
    return user


class _FakeProvider:
    """Records the last prompt; returns a fixed reply / structured turn."""

    def __init__(
        self,
        reply: str = "ok-reply",
        turn: AssistantTurn | None = None,
    ) -> None:
        self.reply = reply
        self.turn = turn or AssistantTurn(mode="answer", reply=reply)
        self.system: str | None = None
        self.messages: list[dict[str, str]] | None = None
        self.embed_calls = 0

    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        self.system = system
        self.messages = list(messages)
        return self.reply

    async def chat_structured(
        self, *, system: str, messages: list[dict[str, str]], schema
    ):
        self.system = system
        self.messages = list(messages)
        return self.turn

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        self.embed_calls += 1
        return [0.0] * EMBEDDING_DIMENSIONS


class _FailingProvider:
    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        raise AIProviderError("chat completion failed: down")

    async def chat_structured(
        self, *, system: str, messages: list[dict[str, str]], schema
    ):
        raise AIProviderError("structured completion failed: down")

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


async def test_recent_entities_in_context(session: AsyncSession) -> None:
    """Entities outside the today / 7-day windows are still referenceable
    via the 'recently touched' context sections (no tool round-trip)."""
    user = await _user(session)
    today = datetime.now(tz=UTC).date()
    standup = await calendar_service.create_item(
        session,
        user,
        title="Team standup",
        kind=ItemKind.event,
        starts_at=datetime.combine(today, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=12),
    )
    # 30 days out: outside today and the 7-day upcoming window.
    far = await calendar_service.create_item(
        session,
        user,
        title="Far meeting",
        starts_at=datetime.now(tz=UTC) + timedelta(days=30),
    )
    # Five soon-firing reminders fill ctx.reminders (fire_at order, limit 5);
    # the sixth, created last and firing far out, must come from the
    # recent-created section.
    for n in range(5):
        await reminders_service.create_reminder(
            session,
            user,
            fire_at=datetime.now(tz=UTC) + timedelta(hours=n + 1),
            message=f"Soon {n}",
        )
    distant = await reminders_service.create_reminder(
        session,
        user,
        fire_at=datetime.now(tz=UTC) + timedelta(days=60),
        message="Distant one",
    )
    await session.commit()

    ctx = await chat_service.build_context(
        session, user, "move it", provider=_FakeProvider()
    )
    assert [i.title for i in ctx.today_items] == ["Team standup"]
    assert [i.title for i in ctx.recent_items] == ["Far meeting"]
    assert [r.message for r in ctx.recent_reminders] == ["Distant one"]

    rendered = chat_service.render_context(ctx)
    assert "Recently touched items" in rendered
    assert "Recently created reminders" in rendered
    recent_block = rendered.split("Recently touched items", 1)[1].split("\n\n")[0]
    assert f"id={far.id} Far meeting" in recent_block
    assert "status: scheduled" in recent_block
    # Deduped: the today item is not repeated in the recent section.
    assert f"id={standup.id}" not in recent_block
    assert f"id={distant.id} Distant one" in rendered


async def test_render_context_empty_is_placeholder(session: AsyncSession) -> None:
    user = await _user(session)
    ctx = await chat_service.build_context(session, user, "hi", provider=_FakeProvider())
    assert chat_service.render_context(ctx) == "(no additional context)"


async def test_build_context_retrieve_only_when_requested(session: AsyncSession) -> None:
    """retrieve=False (default) never embeds; retrieve=True pulls excerpts
    and deterministic citations (SPEC §6)."""
    from test_files import _indexed_file  # noqa: PLC0415

    user = await _user(session)
    await _indexed_file(
        session,
        user,
        filename="warranty.txt",
        texts=["the warranty covers repairs for two years"],
    )
    provider = _FakeProvider()

    ctx_default = await chat_service.build_context(session, user, "hello", provider=provider)
    assert ctx_default.file_excerpts == []
    assert provider.embed_calls == 0

    ctx = await chat_service.build_context(
        session, user, "warranty repairs", provider=provider, retrieve=True
    )
    assert len(ctx.file_excerpts) == 1
    assert ctx.citations == "Sources: warranty.txt"


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


class _EmbeddingDownProvider(_FakeProvider):
    """Chat works, but every embedding call fails (outage simulation)."""

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        raise AIProviderError("embedding server down")

    async def embed_query(self, *, query: str) -> list[float]:
        raise AIProviderError("embedding server down")


async def test_embedding_outage_does_not_break_unrelated_chat(
    session: AsyncSession,
) -> None:
    """Ordinary chat never calls the embedding provider, so an embedding
    outage cannot break it (SPEC §6, §13)."""
    user = await _user(session)
    provider = _EmbeddingDownProvider(reply="hi there!")

    reply = await chat_service.chat(session, user, "hello", provider=provider)
    await session.commit()

    assert reply == "hi there!"
    assert provider.embed_calls == 0
    assert (
        len((await session.scalars(select(ChatMessage))).all()) == 2
    )


# ---------------------------------------------------------------------------
# Bot wiring
# ---------------------------------------------------------------------------


def _fake_text_message(text: str, user_id: int = 71) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(
            id=user_id, first_name="F", last_name=None, username=None, is_bot=False
        ),
        chat=SimpleNamespace(id=100),
        bot=SimpleNamespace(id=777, send_chat_action=AsyncMock()),
        answer=AsyncMock(),
    )


def _fake_state() -> SimpleNamespace:
    return SimpleNamespace(
        get_state=AsyncMock(return_value=None),
        set_state=AsyncMock(),
        clear=AsyncMock(),
        update_data=AsyncMock(),
    )


def _no_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests assert a single outgoing message; the thinking UX
    # (a temporary status message) is covered in tests/test_thinking_ux.py.
    monkeypatch.setattr(
        handlers.common,
        "get_settings",
        lambda: SimpleNamespace(
            chat_thinking_enabled=False, public_base_url="https://app.test"
        ),
    )


async def test_on_text_uses_contextual_chat(session: AsyncSession, monkeypatch) -> None:
    _no_thinking(monkeypatch)
    monkeypatch.setattr(
        turns_service, "get_ai_provider", lambda: _FakeProvider(reply="chat-reply")
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

    _no_thinking(monkeypatch)
    monkeypatch.setattr(
        turns_service, "get_ai_provider", lambda: _FailingProvider()
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
        turns_service, "get_ai_provider", lambda: _FakeProvider(reply="chat-reply")
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


class _TxSpyProvider:
    """Records the session's transaction state at the moment of model I/O."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.in_transaction_during_call: bool | None = None

    async def chat_structured(self, *, system: str, messages, schema):
        self.in_transaction_during_call = self._session.in_transaction()
        return AITaskDraft(title="Call mom", kind="task")

    async def chat(self, *, system: str, messages) -> str:
        return "ok"

    async def embed_documents(self, *, texts):
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]

    async def embed_query(self, *, query):
        return [0.0] * EMBEDDING_DIMENSIONS


async def test_natural_language_draft_releases_tx_before_model_io(
    session: AsyncSession, monkeypatch
) -> None:
    """The NL draft branch must not hold a DB transaction across the model's
    network call (V4 §19-20): commit the session before chat_structured."""
    _no_thinking(monkeypatch)
    provider = _TxSpyProvider(session)
    monkeypatch.setattr(
        handlers.chat, "get_ai_provider", lambda: provider
    )
    state = SimpleNamespace(
        get_state=AsyncMock(return_value=TaskDraftStates.waiting_for_text),
        set_state=AsyncMock(),
        clear=AsyncMock(),
        update_data=AsyncMock(),
    )
    # No `title:` line -> _parse_draft raises ValueError -> NL/model branch.
    message = _fake_text_message("remind me to call mom tomorrow")
    await on_text(message, session, state)
    await session.commit()

    assert provider.in_transaction_during_call is False
    state.set_state.assert_awaited_once_with(TaskDraftStates.confirm)


async def test_on_text_renders_model_markdown_for_telegram(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the model reply is rendered; the bot has no global parse mode."""
    _no_thinking(monkeypatch)
    monkeypatch.setattr(
        turns_service,
        "get_ai_provider",
        lambda: _FakeProvider(reply="**Bold** and `code`"),
    )
    message = _fake_text_message("format this")
    await on_text(message, session, _fake_state())
    await session.commit()

    message.answer.assert_awaited_once()
    call = message.answer.await_args
    assert call.args[0] == "<b>Bold</b> and <code>code</code>"
    assert call.kwargs["parse_mode"] == "HTML"
