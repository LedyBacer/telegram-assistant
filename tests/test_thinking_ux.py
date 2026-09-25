"""Configurable chat timeout, Qwen thinking mode, and the Telegram UX
around them (SPEC §6, §15).

Covers: the default/configured chat timeout reaching the chat client, the
embedding provider staying on its own timeout, the explicit
``chat_template_kwargs.enable_thinking`` request option for both normal
chat and structured completion, timeout failing cleanly without a second
long inference, and the temporary localized "Думаю…" / "Thinking…" status
message lifecycle (sent only when thinking is enabled, deleted on success,
provider error, and timeout; deletion failures never breaking the flow).

All Telegram and AI HTTP calls are faked; no network access is required.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APITimeoutError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.ai import (
    AIProviderError,
    AITaskDraft,
    AITimeoutError,
    OpenAIChatProvider,
    build_ai_provider,
)
from assistant.ai.schemas import AssistantTurn
from assistant.bot import handlers
from assistant.bot.handlers import on_text
from assistant.bot.states import TaskDraftStates
from assistant.config import Settings
from assistant.services import turns as turns_service
from assistant.services.users import upsert_user

# ---------------------------------------------------------------------------
# Settings: chat timeout and thinking mode
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    base = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "public_base_url": "https://app.test",
        "telegram_bot_token": "1:test",
        "chat_base_url": "https://chat.example/v1",
        "chat_api_key": "chat-key",
        "embedding_base_url": "https://embed.example/v1",
        "embedding_api_key": "embed-key",
        # Hermetic: pin the thinking profile so a live-eval .env (which sets
        # CHAT_THINKING_ENABLED / CHAT_THINKING_BUDGET_TOKENS) cannot leak in.
        # Init kwargs take precedence over the .env file in pydantic-settings.
        "chat_thinking_enabled": False,
        "chat_thinking_budget_tokens": None,
        "chat_reasoning_effort": None,
    }
    base.update(overrides)
    return Settings(**base)


def test_settings_chat_timeout_default_is_180_seconds() -> None:
    assert _settings().chat_timeout_seconds == 180.0


def test_settings_chat_thinking_default_is_disabled() -> None:
    # §2: fast, non-thinking is the default; a deployment opts in via
    # CHAT_THINKING_ENABLED=true (and optionally CHAT_REASONING_EFFORT).
    assert _settings().chat_thinking_enabled is False


def test_settings_rejects_unsensible_chat_timeout() -> None:
    for bad in (0, -5, 99999):
        with pytest.raises(ValidationError):
            _settings(chat_timeout_seconds=bad)


# ---------------------------------------------------------------------------
# Provider: timeout reaches the chat client, embeddings stay unaffected
# ---------------------------------------------------------------------------


def test_chat_provider_default_timeout_is_180() -> None:
    provider = OpenAIChatProvider(api_key="k")
    assert provider._timeout == 180.0
    assert provider._client.timeout.read == 180.0


def test_configured_chat_timeout_reaches_the_client() -> None:
    provider = OpenAIChatProvider(api_key="k", timeout=300.0)
    # A non-streaming completion is one read phase: the configured value
    # bounds the whole generation.
    assert provider._client.timeout.read == 300.0
    assert provider._client.timeout.connect == 10.0


def test_build_ai_provider_applies_chat_settings() -> None:
    provider = build_ai_provider(
        _settings(chat_timeout_seconds=300, chat_thinking_enabled=False)
    )
    chat_client = provider._chat_provider._client
    assert chat_client.timeout.read == 300.0
    assert provider._chat_provider._thinking_enabled is False


def test_embedding_provider_timeout_is_unaffected_by_chat_timeout() -> None:
    provider = build_ai_provider(_settings(chat_timeout_seconds=300))
    embed_client = provider._embedding_provider._client
    # Embeddings keep their own short timeout; the chat setting must not
    # leak into them.
    assert embed_client.timeout == 60.0


# ---------------------------------------------------------------------------
# Provider: explicit llama.cpp thinking option on every chat call
# ---------------------------------------------------------------------------


def _fake_completion(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _chat_provider(thinking: bool = True) -> tuple[OpenAIChatProvider, AsyncMock]:
    provider = OpenAIChatProvider(api_key="k", thinking_enabled=thinking)
    create = AsyncMock(return_value=_fake_completion('{"title": "ok"}'))
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider, create


def test_chat_thinking_enabled_sends_llamacpp_option() -> None:
    provider, create = _chat_provider(thinking=True)

    asyncio.run(
        provider.chat(system="S", messages=[{"role": "user", "content": "hi"}])
    )

    kwargs = create.await_args.kwargs
    assert kwargs["temperature"] == 1.0
    assert kwargs["top_p"] == 0.95
    assert kwargs["presence_penalty"] == 1.5
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": True},
        "top_k": 20,
        "min_p": 0.0,
        "repeat_penalty": 1.0,
    }


def test_chat_thinking_disabled_sends_no_thinking_option() -> None:
    provider, create = _chat_provider(thinking=False)

    asyncio.run(
        provider.chat(system="S", messages=[{"role": "user", "content": "hi"}])
    )

    kwargs = create.await_args.kwargs
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_thinking_option_applies_to_structured_completion() -> None:
    provider, create = _chat_provider(thinking=False)
    create.return_value = _fake_completion('{"title": "Draft", "kind": "task"}')

    out = asyncio.run(
        provider.chat_structured(
            system="S",
            messages=[{"role": "user", "content": "remind me"}],
            schema=AITaskDraft,
        )
    )

    assert out.title == "Draft"
    kwargs = create.await_args.kwargs
    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


# ---------------------------------------------------------------------------
# Provider: timeout fails cleanly, no second long inference
# ---------------------------------------------------------------------------


def _timeout_error() -> APITimeoutError:
    return APITimeoutError(request=httpx.Request("POST", "https://x.test"))


def test_chat_provider_timeout_raises_aitimeout_error() -> None:
    provider, create = _chat_provider()
    create.side_effect = _timeout_error()

    with pytest.raises(AITimeoutError, match="timed out"):
        asyncio.run(
            provider.chat(system="S", messages=[{"role": "user", "content": "x"}])
        )


def test_chat_structured_timeout_fails_cleanly_without_retry() -> None:
    """A full inference timeout must not immediately start a second equally
    long inference: exactly one provider call is made."""
    provider, create = _chat_provider()
    create.side_effect = _timeout_error()

    with pytest.raises(AITimeoutError, match="timed out"):
        asyncio.run(
            provider.chat_structured(
                system="S",
                messages=[{"role": "user", "content": "remind me at 17:00"}],
                schema=AITaskDraft,
            )
        )
    assert create.await_count == 1


def test_aitimeout_error_is_an_ai_provider_error() -> None:
    # The bot's existing `except AIProviderError` fallbacks keep catching
    # timeouts.
    assert issubclass(AITimeoutError, AIProviderError)


# ---------------------------------------------------------------------------
# Bot UX: temporary localized "Thinking…" status message
# ---------------------------------------------------------------------------


def _fake_tg_user(user_id: int = 91) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id, first_name="T", last_name=None, username="t", is_bot=False
    )


def _make_message(text: str) -> tuple[SimpleNamespace, list[SimpleNamespace]]:
    sent: list[SimpleNamespace] = []

    def _answer(*args: Any, **kwargs: Any) -> SimpleNamespace:
        message = SimpleNamespace(delete=AsyncMock(), text=args[0] if args else None)
        sent.append(message)
        return message

    message = SimpleNamespace(
        text=text,
        from_user=_fake_tg_user(),
        answer=AsyncMock(side_effect=_answer),
    )
    return message, sent


def _make_message_with_failing_delete(
    text: str,
) -> tuple[SimpleNamespace, list[SimpleNamespace]]:
    sent: list[SimpleNamespace] = []

    def _answer(*args: Any, **kwargs: Any) -> SimpleNamespace:
        message = SimpleNamespace(
            delete=AsyncMock(side_effect=RuntimeError("telegram gone")),
            text=args[0] if args else None,
        )
        sent.append(message)
        return message

    message = SimpleNamespace(
        text=text,
        from_user=_fake_tg_user(),
        answer=AsyncMock(side_effect=_answer),
    )
    return message, sent


def _fake_settings(thinking: bool) -> SimpleNamespace:
    return SimpleNamespace(
        chat_thinking_enabled=thinking, public_base_url="https://app.test"
    )


async def _user_with_lang(session: AsyncSession, lang: str) -> None:
    user, _ = await upsert_user(session, user_id=91, first_name="T")
    user.settings.language = lang
    await session.commit()


class _DraftProvider:
    def __init__(
        self,
        draft: AITaskDraft | None = None,
        error: Exception | None = None,
    ) -> None:
        self.draft = draft
        self.error = error
        self.calls = 0

    async def chat_structured(
        self, *, system: str, messages: list[dict[str, str]], schema: type[Any]
    ) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.draft is not None
        return self.draft


class _ChatProvider:
    def __init__(self, reply: str = "ok-reply", error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls = 0

    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.reply

    async def chat_structured(
        self, *, system: str, messages: list[dict[str, str]], schema: type[Any]
    ) -> Any:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return AssistantTurn(mode="answer", reply=self.reply)

    # Context assembly embeds the query even when the user has no files.
    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 384 for _ in texts]

    async def embed_query(self, *, query: str) -> list[float]:
        return [0.0] * 384


def test_ai_draft_duration_maps_to_ends_at_not_due_at() -> None:
    """SPEC §4.1: a duration extends the item (start -> end) and must NOT be
    stored as a due date."""
    from zoneinfo import ZoneInfo

    from assistant.bot.handlers import _ai_draft_to_task_draft

    draft = _ai_draft_to_task_draft(
        AITaskDraft(
            title="Gym",
            kind="event",
            start=datetime(2026, 9, 25, 20, 0),
            duration_minutes=60,
        ),
        ZoneInfo("Europe/Berlin"),
    )
    # Naive 20:00 Berlin (CEST) is 18:00 UTC; +60 min -> 19:00 UTC end.
    assert draft.ends_at == datetime(2026, 9, 25, 19, 0, tzinfo=UTC)
    assert draft.due_at is None


def _draft_provider() -> _DraftProvider:
    return _DraftProvider(
        draft=AITaskDraft(
            title="Позвонить Сергею",
            kind="task",
            start=datetime(2026, 9, 22, 17, 0),
            reminder_offsets=[0],
        )
    )


def _fake_state(state_value: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        get_state=AsyncMock(return_value=state_value),
        get_data=AsyncMock(return_value={}),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )


DRAFT_STATE = TaskDraftStates.waiting_for_text.state


async def test_draft_russian_user_sees_thinking_status(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _user_with_lang(session, "ru")
    monkeypatch.setattr(handlers.common, "get_settings", lambda: _fake_settings(True))
    monkeypatch.setattr(handlers.chat, "get_ai_provider", lambda: _draft_provider())
    message, sent = _make_message("Сегодня напомни мне позвонить Сергею в 17:00")
    state = _fake_state(DRAFT_STATE)

    await on_text(message, session, state)

    # First outgoing message is the localized status, second the preview.
    assert sent[0].text == "Думаю…"
    assert len(sent) == 2
    # The status message was removed.
    sent[0].delete.assert_awaited_once()
    state.set_state.assert_awaited_once_with(TaskDraftStates.confirm)
    assert "Позвонить Сергею" in sent[1].text


async def test_draft_english_user_sees_thinking_status(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _user_with_lang(session, "en")
    monkeypatch.setattr(handlers.common, "get_settings", lambda: _fake_settings(True))
    monkeypatch.setattr(handlers.chat, "get_ai_provider", lambda: _draft_provider())
    message, sent = _make_message("Remind me to call Sergey today at 17:00")
    state = _fake_state(DRAFT_STATE)

    await on_text(message, session, state)

    assert sent[0].text == "Thinking…"
    sent[0].delete.assert_awaited_once()
    state.set_state.assert_awaited_once_with(TaskDraftStates.confirm)


async def test_draft_no_status_message_when_thinking_disabled(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _user_with_lang(session, "ru")
    monkeypatch.setattr(handlers.common, "get_settings", lambda: _fake_settings(False))
    monkeypatch.setattr(handlers.chat, "get_ai_provider", lambda: _draft_provider())
    message, sent = _make_message("Сегодня напомни мне позвонить Сергею в 17:00")
    state = _fake_state(DRAFT_STATE)

    await on_text(message, session, state)

    # Only the draft preview; no temporary status at all.
    assert len(sent) == 1
    assert "Думаю…" not in (sent[0].text or "")
    state.set_state.assert_awaited_once_with(TaskDraftStates.confirm)


async def test_draft_status_deleted_after_provider_error(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _user_with_lang(session, "ru")
    monkeypatch.setattr(handlers.common, "get_settings", lambda: _fake_settings(True))
    monkeypatch.setattr(
        handlers.chat,
        "get_ai_provider",
        lambda: _DraftProvider(error=AIProviderError("provider down")),
    )
    message, sent = _make_message("Сегодня напомни мне позвонить Сергею в 17:00")
    state = _fake_state(DRAFT_STATE)

    await on_text(message, session, state)

    assert sent[0].text == "Думаю…"
    sent[0].delete.assert_awaited_once()
    # Localized failure, no raw provider text.
    assert "Не удалось понять" in sent[1].text
    state.update_data.assert_not_awaited()


async def test_draft_status_deleted_after_timeout(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _user_with_lang(session, "ru")
    monkeypatch.setattr(handlers.common, "get_settings", lambda: _fake_settings(True))
    monkeypatch.setattr(
        handlers.chat,
        "get_ai_provider",
        lambda: _DraftProvider(error=AITimeoutError("timed out after 180 s")),
    )
    message, sent = _make_message("Сегодня напомни мне позвонить Сергею в 17:00")
    state = _fake_state(DRAFT_STATE)

    await on_text(message, session, state)

    assert sent[0].text == "Думаю…"
    sent[0].delete.assert_awaited_once()
    assert "Не удалось понять" in sent[1].text


async def test_draft_delete_failure_does_not_break_the_flow(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _user_with_lang(session, "ru")
    monkeypatch.setattr(handlers.common, "get_settings", lambda: _fake_settings(True))
    monkeypatch.setattr(handlers.chat, "get_ai_provider", lambda: _draft_provider())
    message, sent = _make_message_with_failing_delete(
        "Сегодня напомни мне позвонить Сергею в 17:00"
    )
    state = _fake_state(DRAFT_STATE)

    # The flow completes: preview is shown even though the status
    # deletion failed.
    await on_text(message, session, state)
    assert len(sent) == 2
    state.set_state.assert_awaited_once_with(TaskDraftStates.confirm)
    assert "Позвонить Сергею" in sent[1].text


async def test_chat_thinking_status_lifecycle(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _user_with_lang(session, "en")
    monkeypatch.setattr(handlers.common, "get_settings", lambda: _fake_settings(True))
    provider = _ChatProvider(reply="hello back")
    monkeypatch.setattr(turns_service, "get_ai_provider", lambda: provider)
    message, sent = _make_message("what's on my plate today?")
    state = _fake_state(None)

    await on_text(message, session, state)

    assert provider.calls == 1
    assert sent[0].text == "Thinking…"
    sent[0].delete.assert_awaited_once()
    assert sent[1].text == "hello back"


async def test_chat_no_status_when_thinking_disabled(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _user_with_lang(session, "ru")
    monkeypatch.setattr(handlers.common, "get_settings", lambda: _fake_settings(False))
    monkeypatch.setattr(turns_service, "get_ai_provider", lambda: _ChatProvider())
    message, sent = _make_message("привет")
    state = _fake_state(None)

    await on_text(message, session, state)

    assert len(sent) == 1
    assert sent[0].text == "ok-reply"


async def test_chat_status_deleted_on_provider_error(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _user_with_lang(session, "ru")
    monkeypatch.setattr(handlers.common, "get_settings", lambda: _fake_settings(True))
    monkeypatch.setattr(
        turns_service,
        "get_ai_provider",
        lambda: _ChatProvider(error=AITimeoutError("timed out after 180 s")),
    )
    message, sent = _make_message("привет")
    state = _fake_state(None)

    await on_text(message, session, state)

    assert sent[0].text == "Думаю…"
    sent[0].delete.assert_awaited_once()
    assert "Сейчас не могу связаться" in sent[1].text

def test_qwen35_structured_thinking_uses_precise_sampling_profile() -> None:
    provider, create = _chat_provider(thinking=True)
    create.return_value = _fake_completion('{"title": "Draft", "kind": "task"}')
    out = asyncio.run(
        provider.chat_structured(
            system="S",
            messages=[{"role": "user", "content": "remind me"}],
            schema=AITaskDraft,
        )
    )
    assert out.title == "Draft"
    kwargs = create.await_args.kwargs
    assert kwargs["temperature"] == 0.6
    assert kwargs["top_p"] == 0.95
    assert kwargs["presence_penalty"] == 0.0
    assert kwargs["extra_body"]["top_k"] == 20
    assert kwargs["extra_body"]["min_p"] == 0.0


def test_thinking_budget_is_forwarded_per_request() -> None:
    provider = OpenAIChatProvider(
        api_key="k",
        thinking_enabled=True,
        thinking_budget_tokens=4096,
    )
    create = AsyncMock(return_value=_fake_completion("ok"))
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    asyncio.run(provider.chat(system="S", messages=[{"role": "user", "content": "hi"}]))
    assert create.await_args.kwargs["extra_body"]["thinking_budget_tokens"] == 4096


def test_settings_thinking_budget_is_optional_and_bounded() -> None:
    assert _settings().chat_thinking_budget_tokens is None
    assert _settings(chat_thinking_budget_tokens=4096).chat_thinking_budget_tokens == 4096
    for bad in (0, -1, 32769):
        with pytest.raises(ValidationError):
            _settings(chat_thinking_budget_tokens=bad)
