"""AI provider abstraction and structured task-draft tests (SPEC §6, §15, §30).

External model calls are faked: the OpenAI SDK clients are mocked for provider
unit tests and a hand-rolled fake provider is used for the bot flow. No real
credentials or network access are required.

Covers the split-provider design: chat and embeddings use independent
OpenAI-compatible clients with independent base URLs, API keys, and models;
E5 prefixes are applied centrally by the embedding provider; embedding
dimensions are validated against the configured value.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest
from openai import APIError
from pydantic import ValidationError
from sqlalchemy import text

from assistant.ai import (
    E5_DOCUMENT_PREFIX,
    E5_QUERY_PREFIX,
    AIOutputValidationError,
    AIProviderError,
    AITaskDraft,
    EmbeddingDimensionError,
    OpenAIChatProvider,
    OpenAICompatibleProvider,
    OpenAIEmbeddingProvider,
    build_ai_provider,
    extract_json_object,
)
from assistant.ai.schemas import ActionProposal, AssistantFold, AssistantTurn
from assistant.bot import handlers
from assistant.bot.handlers import on_draft, on_text
from assistant.bot.states import TaskDraftStates
from assistant.config import Settings
from assistant.services.users import upsert_user

# ---------------------------------------------------------------------------
# Provider unit tests (mocked AsyncOpenAI clients)
# ---------------------------------------------------------------------------


def _fake_completion(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _fake_embedding_response(vectors: list[list[float]]) -> Any:
    return SimpleNamespace(data=[SimpleNamespace(embedding=list(v)) for v in vectors])


def _chat_provider_with_fake_client(
    model: str = "fake-model",
) -> tuple[OpenAIChatProvider, AsyncMock]:
    provider = OpenAIChatProvider(api_key="chat-key", model=model)
    create = AsyncMock(return_value=_fake_completion('{"title": "ok"}'))
    provider._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return provider, create


def _embedding_provider_with_fake_client(
    model: str = "fake-embed-model",
    dimensions: int = 384,
) -> tuple[OpenAIEmbeddingProvider, AsyncMock]:
    provider = OpenAIEmbeddingProvider(
        api_key="embed-key", model=model, dimensions=dimensions
    )
    create = AsyncMock(
        return_value=_fake_embedding_response([[0.1] * dimensions])
    )
    provider._client = SimpleNamespace(embeddings=SimpleNamespace(create=create))
    return provider, create


def _api_error() -> APIError:
    return APIError("boom", httpx.Request("POST", "https://x.test"), body=None)


def test_chat_provider_prefends_system_and_returns_content() -> None:
    provider, create = _chat_provider_with_fake_client()
    create.return_value = _fake_completion("hello back")

    out = asyncio.run(
        provider.chat(system="SYS", messages=[{"role": "user", "content": "hi"}])
    )

    assert out == "hello back"
    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "fake-model"
    assert kwargs["messages"][0] == {"role": "system", "content": "SYS"}
    assert kwargs["messages"][1] == {"role": "user", "content": "hi"}
    # SPEC §15: avoid provider-side storage of conversations.
    assert kwargs["store"] is False


def test_chat_provider_wraps_api_errors() -> None:
    provider, create = _chat_provider_with_fake_client()
    create.side_effect = _api_error()
    with pytest.raises(AIProviderError, match="chat completion failed"):
        asyncio.run(
            provider.chat(system="S", messages=[{"role": "user", "content": "x"}])
        )


def test_chat_structured_success() -> None:
    provider, create = _chat_provider_with_fake_client()
    create.return_value = _fake_completion('{"title": "Call Alex", "kind": "task"}')

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
    # llama.cpp /v1 does not support OpenAI-only response_format=json_schema:
    # the JSON contract travels in the system prompt instead.
    assert "response_format" not in kwargs
    assert "ONLY a single valid JSON object" in kwargs["messages"][0]["content"]
    assert kwargs["temperature"] == 0
    # SPEC §15: avoid provider-side storage of conversations.
    assert kwargs["store"] is False


def test_chat_structured_retries_on_invalid_then_succeeds() -> None:
    provider, create = _chat_provider_with_fake_client()
    create.side_effect = [
        _fake_completion("not json at all"),
        _fake_completion('{"title": "Valid"}'),
    ]

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
    provider, create = _chat_provider_with_fake_client()
    create.side_effect = [
        _fake_completion("nope"),
        _fake_completion('{"title": "", "kind": "task"}'),  # title too short
    ]

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
    provider, create = _chat_provider_with_fake_client()
    create.side_effect = _api_error()

    with pytest.raises(AIProviderError, match="structured completion failed"):
        asyncio.run(
            provider.chat_structured(
                system="S",
                messages=[{"role": "user", "content": "x"}],
                schema=AITaskDraft,
            )
        )


# ---------------------------------------------------------------------------
# Sampling profiles (V5.4 §4): the model-card family must apply by an
# explicit profile (not the alias string) and be independent of thinking mode.
# ---------------------------------------------------------------------------


def _provider_with_profile(
    *,
    model: str = "ornith1.5-9b-q5km-64k",
    profile: str = "auto",
    structured_sampling: str = "precise",
    thinking_enabled: bool = False,
) -> OpenAIChatProvider:
    return OpenAIChatProvider(
        api_key="chat-key",
        model=model,
        thinking_enabled=thinking_enabled,
        sampling_profile=profile,
        structured_sampling=structured_sampling,
    )


def test_sampling_auto_resolves_reasoning_families() -> None:
    assert (
        _provider_with_profile(model="ornith1.5-9b-q5km-64k")
        ._resolve_sampling_profile()
        == "qwen35_reasoning"
    )
    assert (
        _provider_with_profile(model="qwen3.5-9b-64k")._resolve_sampling_profile()
        == "qwen35_reasoning"
    )
    assert (
        _provider_with_profile(model="Qwen3.5-9B")._resolve_sampling_profile()
        == "qwen35_reasoning"
    )
    assert (
        _provider_with_profile(model="qwen3_5-9b")._resolve_sampling_profile()
        == "qwen35_reasoning"
    )


def test_sampling_auto_unknown_model_is_legacy() -> None:
    assert (
        _provider_with_profile(model="fake-model")._resolve_sampling_profile()
        == "legacy"
    )
    assert (
        _provider_with_profile(model="llama-3.1-8b")._resolve_sampling_profile()
        == "legacy"
    )


def test_sampling_explicit_profile_wins_over_alias() -> None:
    # Explicit legacy even though the alias would auto-resolve to reasoning.
    assert (
        _provider_with_profile(
            model="ornith1.5-9b-q5km-64k", profile="legacy"
        )._resolve_sampling_profile()
        == "legacy"
    )
    # Explicit reasoning even though the alias would not auto-resolve to it.
    assert (
        _provider_with_profile(
            model="fake-model", profile="qwen35_reasoning"
        )._resolve_sampling_profile()
        == "qwen35_reasoning"
    )


def test_reasoning_profile_general_chat_sampling() -> None:
    provider = _provider_with_profile(model="ornith1.5-9b-q5km-64k")
    opts = provider._request_options(structured=False)
    assert opts["temperature"] == 1.0
    assert opts["top_p"] == 0.95
    assert opts["presence_penalty"] == 1.5
    extra = opts["extra_body"]
    assert extra["top_k"] == 20
    assert extra["min_p"] == 0.0
    assert extra["repeat_penalty"] == 1.0


def test_reasoning_profile_structured_sampling_precise_and_general() -> None:
    precise = _provider_with_profile(
        model="ornith1.5-9b-q5km-64k", structured_sampling="precise"
    )._request_options(structured=True)
    assert precise["temperature"] == 0.6
    assert precise["presence_penalty"] == 0.0
    assert precise["top_p"] == 0.95
    assert precise["extra_body"]["top_k"] == 20

    general = _provider_with_profile(
        model="ornith1.5-9b-q5km-64k", structured_sampling="general"
    )._request_options(structured=True)
    assert general["temperature"] == 1.0
    assert general["presence_penalty"] == 1.5


def test_reasoning_profile_sampling_independent_of_thinking() -> None:
    off = _provider_with_profile(model="ornith1.5-9b-q5km-64k", thinking_enabled=False)
    on = _provider_with_profile(model="ornith1.5-9b-q5km-64k", thinking_enabled=True)
    off_opts = off._request_options(structured=False)
    on_opts = on._request_options(structured=False)
    # Identical sampling with thinking on and off (the A/B study toggles only
    # enable_thinking).
    for key in ("temperature", "top_p", "presence_penalty"):
        assert off_opts[key] == on_opts[key]
    for key in ("top_k", "min_p", "repeat_penalty"):
        assert off_opts["extra_body"][key] == on_opts["extra_body"][key]
    # Only enable_thinking differs.
    assert off_opts["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False
    assert on_opts["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


def test_legacy_profile_sampling_unchanged() -> None:
    provider = _provider_with_profile(model="fake-model")
    assert provider._request_options(structured=False)["temperature"] == 0.7
    assert provider._request_options(structured=True)["temperature"] == 0
    # The legacy path sends no model-card knobs.
    assert "top_k" not in provider._request_options(structured=False)["extra_body"]


def test_reasoning_profile_sampling_reaches_provider_call() -> None:
    provider, create = _chat_provider_with_fake_client(model="ornith1.5-9b-q5km-64k")
    provider._sampling_profile = "qwen35_reasoning"
    create.return_value = _fake_completion("hi")

    asyncio.run(
        provider.chat(system="S", messages=[{"role": "user", "content": "x"}])
    )
    kwargs = create.await_args.kwargs
    assert kwargs["temperature"] == 1.0
    assert kwargs["top_p"] == 0.95
    assert kwargs["presence_penalty"] == 1.5
    assert kwargs["extra_body"]["top_k"] == 20
    assert kwargs["extra_body"]["min_p"] == 0.0
    assert kwargs["extra_body"]["repeat_penalty"] == 1.0


def test_build_ai_provider_applies_sampling_profile() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        public_base_url="https://app.test",
        telegram_bot_token="1:test",
        chat_base_url="https://chat.example/v1",
        chat_api_key="chat-key",
        chat_model="ornith1.5-9b-q5km-64k",
        chat_sampling_profile="qwen35_reasoning",
        chat_structured_sampling="general",
        embedding_base_url="https://embed.example/v1",
        embedding_api_key="embed-key",
        embedding_model="multilingual-e5-small",
        embedding_dimensions=384,
    )
    provider = build_ai_provider(settings)
    chat = provider._chat_provider
    assert chat._sampling_profile == "qwen35_reasoning"
    assert chat._structured_sampling == "general"
    # The explicit structured family is applied even for the ornith alias.
    opts = chat._request_options(structured=True)
    assert opts["temperature"] == 1.0
    assert opts["presence_penalty"] == 1.5


# ---------------------------------------------------------------------------
# extract_json_object: real Qwen/llama.cpp output shapes
# ---------------------------------------------------------------------------


def test_extract_json_object_bare() -> None:
    assert extract_json_object('{"title": "A", "kind": "task"}') == {
        "title": "A",
        "kind": "task",
    }


def test_extract_json_object_fenced() -> None:
    content = 'Here you go:\n```json\n{"title": "A"}\n```\nHope that helps.'
    assert extract_json_object(content) == {"title": "A"}


def test_extract_json_object_thinking_preamble_and_trailing_prose() -> None:
    content = (
        'The user wants a task to call Sergey at 17:00 today. Let me draft it.\n'
        '{"title": "Позвонить Сергею", "start": "2026-09-22 17:00", '
        '"reminder_offsets": [0]}\n'
        "I resolved the date to today."
    )
    out = extract_json_object(content)
    assert out["title"] == "Позвонить Сергею"
    assert out["reminder_offsets"] == [0]


def test_extract_json_object_ignores_braces_in_prose_and_strings() -> None:
    # Braces in the prose and inside a string value must not break extraction.
    content = (
        'Use the format {"key": "value"} carefully.\n'
        '{"title": "Braces }{ inside", "kind": "event"}'
    )
    out = extract_json_object(content)
    assert out == {"title": "Braces }{ inside", "kind": "event"}


def test_extract_json_object_nested_object() -> None:
    content = 'Sure: {"a": {"b": 1}, "c": [1, 2]} done.'
    assert extract_json_object(content) == {"a": {"b": 1}, "c": [1, 2]}


def test_extract_json_object_no_object_raises() -> None:
    with pytest.raises(ValueError, match="no JSON object"):
        extract_json_object("I don't know what to do with this request.")


def test_extract_json_object_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        extract_json_object("   ")


def test_chat_structured_handles_qwen_preamble_in_single_call() -> None:
    """A realistic Qwen3.5 reply (prose + fenced JSON) parses on attempt 1."""
    provider, create = _chat_provider_with_fake_client(model="qwen3.5-9b-64k")
    create.return_value = _fake_completion(
        'Конечно, оформлю задачу.\n'
        "```json\n"
        '{"title": "Позвонить Сергею", "kind": "task", '
        '"start": "2026-09-22 17:00", "reminder_offsets": [0]}\n'
        "```"
    )

    out = asyncio.run(
        provider.chat_structured(
            system="S",
            messages=[{"role": "user", "content": "Мне нужно сегодня позвонить Сергею в 17:00"}],
            schema=AITaskDraft,
        )
    )
    assert out.title == "Позвонить Сергею"
    assert out.start == datetime(2026, 9, 22, 17, 0)
    assert create.await_count == 1


# ---------------------------------------------------------------------------
# Realistic Qwen-output fixtures through the full chat_structured path
# (V3 P41): the 9B model's malformed-but-recoverable shapes must be handled
# by extraction + validation + the bounded repair loop, and unrecoverable
# contradictions must fail safely (AIOutputValidationError), never pass.
# ---------------------------------------------------------------------------


def test_fixture_contradictory_turn_clarification_with_actions_repairs() -> None:
    """A 9B model that asks for clarification AND proposes a mutation in the
    same turn is contradictory (V3 §8). The first response is rejected by
    the schema; the corrective retry lets the model self-repair."""
    provider, create = _chat_provider_with_fake_client(model="qwen3.5-9b-64k")
    create.side_effect = [
        _fake_completion(
            "Нужно уточнить, какой именно пункт, но я подготовлю перенос.\n"
            "```json\n"
            '{"mode": "clarification", '
            '"clarification": "Какой пункт перенести — утренний или вечерний?", '
            '"actions": [{"kind": "update_item", '
            '"payload": {"item_id": 12, "starts_at": "2026-09-25 18:00"}, '
            '"summary": "Перенести на 18:00"}]}\n'
            "```"
        ),
        _fake_completion(
            "```json\n"
            '{"mode": "clarification", '
            '"clarification": "Какой пункт перенести — утренний или вечерний?"}\n'
            "```"
        ),
    ]

    out = asyncio.run(
        provider.chat_structured(
            system="S",
            messages=[{"role": "user", "content": "перенеси встречу на 18:00"}],
            schema=AssistantTurn,
        )
    )
    assert isinstance(out, AssistantTurn)
    assert out.clarification == "Какой пункт перенести — утренний или вечерний?"
    assert out.actions == []
    assert create.await_count == 2


def test_fixture_contradictory_turn_fails_safely_when_unrepaired() -> None:
    provider, create = _chat_provider_with_fake_client(model="qwen3.5-9b-64k")
    contradictory = (
        '{"mode": "clarification", '
        '"clarification": "Which item do you mean?", '
        '"actions": [{"kind": "complete_item", "payload": {"item_id": 1}, '
        '"summary": "Complete it"}]}'
    )
    create.side_effect = [
        _fake_completion(f"Sure:\n```json\n{contradictory}\n```"),
        _fake_completion(contradictory),
    ]

    with pytest.raises(AIOutputValidationError, match="schema validation"):
        asyncio.run(
            provider.chat_structured(
                system="S",
                messages=[{"role": "user", "content": "move it"}],
                schema=AssistantTurn,
            )
        )
    assert create.await_count == 2


def test_turn_schema_rejects_clarification_with_actions() -> None:
    with pytest.raises(ValidationError, match="must not carry other fields"):
        AssistantTurn(
            mode="clarification",
            clarification="Which one?",
            actions=[
                ActionProposal(
                    kind="complete_item", payload={"item_id": 1}, summary="s"
                )
            ],
        )
    # A blank clarification is not a real clarification: a proposal with a
    # blank clarification field and actions is still valid.
    turn = AssistantTurn(
        mode="proposal",
        clarification="   ",
        actions=[
            ActionProposal(
                kind="complete_item", payload={"item_id": 1}, summary="s"
            )
        ],
    )
    assert len(turn.actions) == 1


def test_turn_schema_infers_mode_from_populated_field() -> None:
    """The live model omits the redundant ``mode`` key when the populated
    field already determines the mode (e.g. ``{"clarification": ...}``);
    validation must infer it instead of rejecting a valid turn as
    ``AIOutputValidationError``."""
    clar = AssistantTurn.model_validate(
        {"clarification": "Сегодня или ближайшая пятница?"}
    )
    assert clar.mode == "clarification"
    assert clar.clarification == "Сегодня или ближайшая пятница?"

    answer = AssistantTurn.model_validate({"reply": "Всё, записал."})
    assert answer.mode == "answer"
    assert answer.reply == "Всё, записал."

    proposal = AssistantTurn.model_validate(
        {
            "actions": [
                {"kind": "complete_item", "payload": {"item_id": 7}, "summary": "Done"}
            ]
        }
    )
    assert proposal.mode == "proposal"
    assert len(proposal.actions) == 1

    fold = AssistantFold.model_validate({"clarification": "Which one?"})
    assert fold.mode == "clarification"
    assert fold.clarification == "Which one?"


def test_fixture_unknown_tool_name_fails_validation() -> None:
    """A tool name outside the Literal fails schema validation; the repair
    prompt offers no new vocabulary, so an un-repaired model fails safely."""
    provider, create = _chat_provider_with_fake_client(model="qwen3.5-9b-64k")
    unknown_tool = (
        '{"mode": "need_data", '
        '"data_requests": [{"tool": "delete_calendar", "query": "gym"}]}'
    )
    create.side_effect = [
        _fake_completion(f"Let me check.\n{unknown_tool}"),
        _fake_completion(unknown_tool),
    ]

    with pytest.raises(AIOutputValidationError, match="schema validation"):
        asyncio.run(
            provider.chat_structured(
                system="S",
                messages=[{"role": "user", "content": "cancel the gym"}],
                schema=AssistantTurn,
            )
        )
    assert create.await_count == 2


def test_fixture_invalid_enum_type_value_repairs() -> None:
    """An out-of-bounds scalar (limit > 20) fails the conint bound on the
    first response; the corrected second response validates."""
    provider, create = _chat_provider_with_fake_client(model="qwen3.5-9b-64k")
    create.side_effect = [
        _fake_completion(
            '{"mode": "need_data", "data_requests": '
            '[{"tool": "calendar", "limit": 99}]}'
        ),
        _fake_completion(
            '{"mode": "need_data", "data_requests": '
            '[{"tool": "calendar", "limit": 5}]}'
        ),
    ]

    out = asyncio.run(
        provider.chat_structured(
            system="S",
            messages=[{"role": "user", "content": "show my calendar"}],
            schema=AssistantTurn,
        )
    )
    assert len(out.data_requests) == 1
    assert out.data_requests[0].limit == 5
    assert create.await_count == 2


def test_fixture_valid_turn_bare_json_single_call() -> None:
    """The common happy path: a bare object, no fences, no preamble — one
    structured call, no repair (cost stays at the 9B budget)."""
    provider, create = _chat_provider_with_fake_client(model="qwen3.5-9b-64k")
    create.return_value = _fake_completion(
        '{"mode": "proposal", '
        '"reply": "Your standup is at 12:00.", '
        '"actions": [{"kind": "complete_item", "payload": {"item_id": 7}, '
        '"summary": "Complete standup"}]}'
    )

    out = asyncio.run(
        provider.chat_structured(
            system="S",
            messages=[{"role": "user", "content": "done with standup"}],
            schema=AssistantTurn,
        )
    )
    assert out.reply == "Your standup is at 12:00."
    assert out.actions[0].kind == "complete_item"
    assert create.await_count == 1


# ---------------------------------------------------------------------------
# Independent provider configuration
# ---------------------------------------------------------------------------


def test_build_ai_provider_uses_independent_clients() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        public_base_url="https://app.test",
        telegram_bot_token="1:test",
        chat_base_url="https://chat.example/v1",
        chat_api_key="chat-key",
        chat_model="qwen3.5-9b-64k",
        embedding_base_url="https://embed.example/v1",
        embedding_api_key="embed-key",
        embedding_model="multilingual-e5-small",
        embedding_dimensions=384,
    )
    provider = build_ai_provider(settings)
    assert isinstance(provider, OpenAICompatibleProvider)
    chat_client = provider._chat_provider._client
    embed_client = provider._embedding_provider._client
    # The SDK normalizes base URLs (trailing slash).
    assert str(chat_client.base_url) == "https://chat.example/v1/"
    assert str(embed_client.base_url) == "https://embed.example/v1/"
    assert chat_client.api_key == "chat-key"
    assert embed_client.api_key == "embed-key"
    assert provider._chat_provider._model == "qwen3.5-9b-64k"
    assert provider._embedding_provider._model == "multilingual-e5-small"
    assert provider._embedding_provider._dimensions == 384


def test_settings_fall_back_to_legacy_openai_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hermetic: ignore the .env file and any ambient provider vars so only the
    # explicit kwargs below apply.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for var in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "CHAT_API_KEY", "CHAT_BASE_URL",
        "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        public_base_url="https://app.test",
        telegram_bot_token="1:test",
        openai_api_key="legacy-key",
        openai_base_url="https://legacy.example/v1",
    )
    assert settings.chat_api_key == "legacy-key"
    assert settings.embedding_api_key == "legacy-key"
    assert settings.chat_base_url == "https://legacy.example/v1"
    assert settings.embedding_base_url == "https://legacy.example/v1"
    provider = build_ai_provider(settings)
    # Both independent clients inherit the legacy endpoint.
    assert str(provider._chat_provider._client.base_url) == "https://legacy.example/v1/"
    assert str(provider._embedding_provider._client.base_url) == "https://legacy.example/v1/"
    assert provider._chat_provider._client.api_key == "legacy-key"
    assert provider._embedding_provider._client.api_key == "legacy-key"


def test_provider_specific_variables_take_precedence_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hermetic: ignore the .env file and any ambient provider vars so only the
    # explicit kwargs below apply.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for var in (
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "CHAT_API_KEY", "CHAT_BASE_URL",
        "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        public_base_url="https://app.test",
        telegram_bot_token="1:test",
        openai_api_key="legacy-key",
        chat_api_key="chat-key",
    )
    assert settings.chat_api_key == "chat-key"
    assert settings.embedding_api_key == "legacy-key"


def test_settings_require_chat_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ignore the .env file so it cannot supply keys this test removes.
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for var in ("OPENAI_API_KEY", "CHAT_API_KEY", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError, match="credentials"):
        Settings(
            database_url="postgresql+asyncpg://u:p@localhost/db",
            public_base_url="https://app.test",
            telegram_bot_token="1:test",
        )


@pytest.mark.asyncio
async def test_settings_allow_chat_only_without_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V3 P42: embeddings are optional; chat stays required."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for var in (
        "OPENAI_API_KEY",
        "EMBEDDING_API_KEY",
        "EMBEDDING_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHAT_API_KEY", "chat-key")
    settings = Settings(
        database_url="postgresql+asyncpg://u:p@localhost/db",
        public_base_url="https://app.test",
        telegram_bot_token="1:test",
    )
    assert settings.chat_api_key == "chat-key"
    assert settings.embedding_api_key is None
    assert settings.embedding_configured is False

    provider = build_ai_provider(settings)
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.embedding_configured is False
    with pytest.raises(AIProviderError, match="not configured"):
        await provider.embed_query(query="x")
    with pytest.raises(AIProviderError, match="not configured"):
        await provider.embed_documents(texts=["x"])


# ---------------------------------------------------------------------------
# Embedding provider: E5 prefixes and dimension validation
# ---------------------------------------------------------------------------


def test_embed_documents_applies_e5_document_prefix() -> None:
    provider, create = _embedding_provider_with_fake_client()
    create.return_value = _fake_embedding_response([[0.1] * 384, [0.2] * 384])

    out = asyncio.run(provider.embed_documents(texts=["chunk one", "chunk two"]))

    kwargs = create.await_args.kwargs
    assert kwargs["model"] == "fake-embed-model"
    # llama.cpp /v1/embeddings: only the OpenAI-compatible fields, no
    # OpenAI-specific options.
    assert set(kwargs) == {"model", "input"}
    assert kwargs["input"] == [
        f"{E5_DOCUMENT_PREFIX}chunk one",
        f"{E5_DOCUMENT_PREFIX}chunk two",
    ]
    assert len(out) == 2


def test_embed_query_applies_e5_query_prefix() -> None:
    provider, create = _embedding_provider_with_fake_client()

    out = asyncio.run(provider.embed_query(query="quarterly plan"))

    assert create.await_args.kwargs["input"] == [f"{E5_QUERY_PREFIX}quarterly plan"]
    assert len(out) == 384


def test_embed_dimension_default_is_384() -> None:
    provider = OpenAIEmbeddingProvider(api_key="k")
    assert provider._dimensions == 384


def test_embed_rejects_unexpected_document_dimension() -> None:
    provider, create = _embedding_provider_with_fake_client(dimensions=384)
    create.return_value = _fake_embedding_response([[0.0] * 512])

    with pytest.raises(EmbeddingDimensionError, match="expected 384, got 512"):
        asyncio.run(provider.embed_documents(texts=["x"]))


def test_embed_rejects_unexpected_query_dimension() -> None:
    provider, create = _embedding_provider_with_fake_client(dimensions=384)
    create.return_value = _fake_embedding_response([[0.0] * 768])

    with pytest.raises(EmbeddingDimensionError, match="expected 384, got 768"):
        asyncio.run(provider.embed_query(query="x"))


def test_embed_wraps_api_errors() -> None:
    provider, create = _embedding_provider_with_fake_client()
    create.side_effect = _api_error()

    with pytest.raises(AIProviderError, match="embedding failed"):
        asyncio.run(provider.embed_documents(texts=["x"]))


def test_composite_provider_routes_calls_to_independent_clients() -> None:
    chat, chat_create = _chat_provider_with_fake_client(model="chat-model")
    chat_create.return_value = _fake_completion("hi there")
    embed, embed_create = _embedding_provider_with_fake_client(model="embed-model")
    provider = OpenAICompatibleProvider(chat=chat, embedding=embed)

    reply = asyncio.run(
        provider.chat(system="s", messages=[{"role": "user", "content": "hi"}])
    )
    assert reply == "hi there"
    assert chat_create.await_args.kwargs["model"] == "chat-model"
    assert embed_create.await_count == 0

    chat_create.return_value = _fake_completion('{"title": "Drafted"}')
    draft = asyncio.run(
        provider.chat_structured(
            system="s", messages=[{"role": "user", "content": "x"}], schema=AITaskDraft
        )
    )
    assert isinstance(draft, AITaskDraft)

    vectors = asyncio.run(provider.embed_documents(texts=["c"]))
    assert len(vectors) == 1
    assert embed_create.await_args.kwargs["model"] == "embed-model"
    assert embed_create.await_args.kwargs["input"] == ["passage: c"]

    vector = asyncio.run(provider.embed_query(query="q"))
    assert len(vector) == 384
    # Each capability only ever hits its own client.
    assert chat_create.await_count == 2
    assert embed_create.await_count == 2


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
    return SimpleNamespace(
        text=text,
        from_user=_fake_tg_user(),
        chat=SimpleNamespace(id=100),
        bot=SimpleNamespace(id=777, send_chat_action=AsyncMock()),
        answer=AsyncMock(),
    )


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
        handlers.chat,
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
    user, _ = await upsert_user(session, user_id=31, first_name="Nat")
    user.settings.language = "en"
    await session.commit()

    message = _fake_message("make it happen sometime")
    # This test asserts a single outgoing message; the thinking status UX
    # is covered in tests/test_thinking_ux.py.
    monkeypatch.setattr(
        handlers.common,
        "get_settings",
        lambda: SimpleNamespace(
            chat_thinking_enabled=False, public_base_url="https://app.test"
        ),
    )
    monkeypatch.setattr(
        handlers.chat,
        "get_ai_provider",
        lambda: _FakeProvider(error=AIProviderError("model down")),
    )
    state = _fake_state(TaskDraftStates.waiting_for_text.state)
    await on_text(message, session, state)

    message.answer.assert_awaited_once()
    assert "couldn't interpret" in message.answer.await_args.args[0]
    state.set_state.assert_not_awaited()
    state.update_data.assert_not_awaited()


# ---------------------------------------------------------------------------
# Relative dates ("today" / "сегодня") resolved in the user's timezone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "language", "tz_name"),
    [
        ("Мне нужно сегодня позвонить Сергею в 17:00", "ru", "Africa/Nairobi"),
        ("Сегодня напомни мне позвонить Сергею в 17:00", "ru", "Europe/Moscow"),
        ("I need to call Sergey today at 17:00", "en", "America/New_York"),
    ],
)
async def test_on_text_relative_today_resolved_in_user_timezone(
    session, monkeypatch, phrase: str, language: str, tz_name: str
) -> None:
    """The model gets the user's TZ + current date in the system prompt and
    returns a *local* naive time; the handler must pin it to the user's
    timezone before persisting UTC."""
    tz = ZoneInfo(tz_name)
    local_start = datetime.combine(datetime.now(tz).date(), time(17, 0))
    user, _ = await upsert_user(session, user_id=31, first_name="Nat")
    user.settings.language = language
    user.settings.timezone = tz_name
    await session.commit()

    monkeypatch.setattr(
        handlers.chat,
        "get_ai_provider",
        lambda: _FakeProvider(
            draft=AITaskDraft(
                title="Позвонить Сергею" if language == "ru" else "Call Sergey",
                start=local_start,  # naive local time, as the model returns it
                reminder_offsets=[0],
            )
        ),
    )
    state = _fake_state(TaskDraftStates.waiting_for_text.state)
    await on_text(_fake_message(phrase), session, state)
    await session.commit()
    updated = state.update_data.await_args.kwargs

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

    item = (await session.execute(text("SELECT starts_at FROM calendar_items"))).one()
    assert item[0] == local_start.replace(tzinfo=tz).astimezone(UTC)
