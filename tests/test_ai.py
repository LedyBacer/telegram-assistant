"""AI provider abstraction and structured task-draft tests (SPEC §6, §15, §30).

External model calls are faked: the OpenAI SDK client is mocked for provider
unit tests and a hand-rolled fake provider is used for the bot flow. No real
credentials or network access are required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import APIError
from sqlalchemy import text

from assistant.ai import (
    AIOutputValidationError,
    AIProviderError,
    AITaskDraft,
    OpenAICompatibleProvider,
    build_ai_provider,
)
from assistant.bot import handlers
from assistant.bot.handlers import on_draft, on_text
from assistant.bot.states import TaskDraftStates
from assistant.config import Settings

# ---------------------------------------------------------------------------
# Provider unit tests (mocked AsyncOpenAI client)
# ---------------------------------------------------------------------------


def _fake_completion(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _provider_with_fake_client() -> tuple[OpenAICompatibleProvider, AsyncMock]:
    provider = OpenAICompatibleProvider(api_key="test-key", chat_model="fake-model")
    create = AsyncMock(return_value=_fake_completion('{"title": "ok"}'))
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider, create


def _api_error() -> APIError:
    return APIError("boom", httpx.Request("POST", "https://x.test"), body=None)


def test_provider_chat_prefends_system_and_returns_content() -> None:
    provider, create = _provider_with_fake_client()
    create.return_value = _fake_completion("hello back")

    result = provider.chat(
        system="SYS", messages=[{"role": "user", "content": "hi"}]
    )

    import asyncio

    out = asyncio.run(result)
    assert out == "hello back"
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "fake-model"
    assert kwargs["messages"][0] == {"role": "system", "content": "SYS"}
    assert kwargs["messages"][1] == {"role": "user", "content": "hi"}


def test_provider_chat_wraps_api_errors() -> None:
    provider, create = _provider_with_fake_client()
    create.side_effect = _api_error()
    with pytest.raises(AIProviderError, match="chat completion failed"):
        import asyncio

        asyncio.run(
            provider.chat(system="S", messages=[{"role": "user", "content": "x"}])
        )


def test_chat_structured_success() -> None:
    provider, create = _provider_with_fake_client()
    create.return_value = _fake_completion('{"title": "Call Alex", "kind": "task"}')

    import asyncio

    out = asyncio.run(
        provider.chat_structured(
            system="S",
            messages=[{"role": "user", "content": "call alex"}],
            schema=AITaskDraft,
        )
    )
    assert isinstance(out, AITaskDraft)
    assert out.title == "Call Alex"
    assert out.kind == "task"
    kwargs = create.await_args.kwargs
    assert kwargs["response_format"]["type"] == "json_schema"
    assert (
        kwargs["response_format"]["json_schema"]["schema"]["properties"]["title"]
        is not None
    )


def test_chat_structured_retries_on_invalid_then_succeeds() -> None:
    provider, create = _provider_with_fake_client()
    create.side_effect = [
        _fake_completion("not json at all"),
        _fake_completion('{"title": "Valid"}'),
    ]
    import asyncio

    out = asyncio.run(
        provider.chat_structured(
            system="S",
            messages=[{"role": "user", "content": "x"}],
            schema=AITaskDraft,
        )
    )
    assert out.title == "Valid"
    assert create.await_count == 2


def test_chat_structured_raises_after_exhausting_attempts() -> None:
    provider, create = _provider_with_fake_client()
    create.side_effect = [
        _fake_completion("nope"),
        _fake_completion('{"title": "", "kind": "task"}'),  # title too short
    ]
    import asyncio

    with pytest.raises(AIOutputValidationError, match="schema validation"):
        asyncio.run(
            provider.chat_structured(
                system="S",
                messages=[{"role": "user", "content": "x"}],
                schema=AITaskDraft,
            )
        )
    assert create.await_count == 2


def test_chat_structured_wraps_api_errors() -> None:
    provider, create = _provider_with_fake_client()
    create.side_effect = _api_error()
    import asyncio

    with pytest.raises(AIProviderError, match="structured completion failed"):
        asyncio.run(
            provider.chat_structured(
                system="S",
                messages=[{"role": "user", "content": "x"}],
                schema=AITaskDraft,
            )
        )


def test_build_ai_provider_from_settings() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        public_base_url="https://app.test",
        telegram_bot_token="1:test",
        openai_api_key="sk-test",
        openai_base_url="https://openai-compatible.example/v1",
        chat_model="test-model",
    )
    provider = build_ai_provider(settings)
    assert isinstance(provider, OpenAICompatibleProvider)
    # The SDK normalizes the base URL (trailing slash).
    assert str(provider._client.base_url) == "https://openai-compatible.example/v1/"


# ---------------------------------------------------------------------------
# Bot flow: natural language -> AI draft -> preview -> confirm
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(
        self,
        draft: AITaskDraft | None = None,
        error: Exception | None = None,
    ) -> None:
        self.draft = draft
        self.error = error
        self.calls = 0

    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        return ""

    async def chat_structured(
        self, *, system: str, messages: list[dict[str, str]], schema: type[Any]
    ) -> Any:
        self.calls += 1
        assert schema is AITaskDraft
        if self.error is not None:
            raise self.error
        assert self.draft is not None
        return self.draft


def _fake_tg_user(user_id: int = 31) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id, first_name="Nat", last_name="Lang", username="nat", is_bot=False
    )


def _fake_message(text: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, from_user=_fake_tg_user(), answer=AsyncMock())


def _fake_state(state_value: str | None = None, data: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_state=AsyncMock(return_value=state_value),
        get_data=AsyncMock(return_value=data or {}),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )


async def _truncate_users(session) -> None:
    await session.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))
    await session.commit()


async def test_on_text_natural_language_uses_ai_draft(session, monkeypatch) -> None:
    monkeypatch.setattr(
        handlers,
        "get_ai_provider",
        lambda: _FakeProvider(
            draft=AITaskDraft(
                title="Call Alex",
                kind="task",
                start=datetime(2026, 9, 23, 18, 30),  # naive -> user TZ (UTC)
                reminder_offsets=[0],
                ambiguities=["time taken as 18:30"],
            )
        ),
    )
    state = _fake_state(TaskDraftStates.waiting_for_text.state)
    await on_text(
        _fake_message("Tomorrow at 18:30 remind me to call Alex"), session, state
    )
    await session.commit()

    state.set_state.assert_awaited_once_with(TaskDraftStates.confirm)
    updated = state.update_data.await_args.kwargs
    assert updated["draft_ai"]["title"] == "Call Alex"

    # Now confirm: the item is persisted from the stored AI draft.
    data_state = _fake_state(data=updated)
    callback = SimpleNamespace(
        from_user=_fake_tg_user(),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    await on_draft(
        callback, handlers.DraftCallback(action="confirm"), session, data_state
    )
    await session.commit()

    item = (
        await session.execute(
            text(
                "SELECT title, kind, starts_at FROM calendar_items"
            )
        )
    ).one()
    assert item[0] == "Call Alex"
    assert item[1] == "task"
    assert item[2] == datetime(2026, 9, 23, 18, 30, tzinfo=UTC)

    reminders = (
        await session.execute(text("SELECT count(*) FROM reminders"))
    ).scalar_one()
    assert reminders == 1


async def test_on_text_ai_failure_shows_help_and_stays_in_flow(
    session, monkeypatch
) -> None:
    message = _fake_message("make it happen sometime")
    monkeypatch.setattr(
        handlers,
        "get_ai_provider",
        lambda: _FakeProvider(error=AIProviderError("model down")),
    )
    state = _fake_state(TaskDraftStates.waiting_for_text.state)
    await on_text(message, session, state)

    message.answer.assert_awaited_once()
    assert "couldn't interpret" in message.answer.await_args.args[0]
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()
