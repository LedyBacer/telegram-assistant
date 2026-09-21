"""OpenAI-compatible chat provider behind a replaceable protocol (SPEC §2, §27).

The SDK client is the only OpenAI-specific code in the package. Transient and
persistent provider failures are surfaced as narrow, meaningful error classes
instead of leaking SDK exceptions to callers.
"""

from __future__ import annotations

import json
from typing import Protocol, TypeVar, runtime_checkable

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# One chat turn: {"role": "system"|"user"|"assistant", "content": str}.
Message = dict[str, str]


class AIProviderError(RuntimeError):
    """The model provider could not be reached or returned an API error."""


class AIOutputValidationError(AIProviderError):
    """The model repeatedly produced output that failed schema validation."""


@runtime_checkable
class AIProvider(Protocol):
    """Interface for chat completion providers."""

    async def chat(self, *, system: str, messages: list[Message]) -> str:
        """Plain conversational completion."""
        ...

    async def chat_structured(
        self, *, system: str, messages: list[Message], schema: type[T]
    ) -> T:
        """Completion whose content is validated against a Pydantic schema."""
        ...

    async def embed(self, *, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts into vectors (one per input, same order)."""
        ...


class OpenAICompatibleProvider:
    """`AsyncOpenAI`-backed provider; any OpenAI-compatible endpoint works."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        chat_model: str = "gpt-4o-mini",
        embedding_model: str = "text-embedding-3-small",
        max_attempts: int = 2,
        timeout: float = 60.0,
    ) -> None:
        self._model = chat_model
        self._embedding_model = embedding_model
        self._max_attempts = max_attempts
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,  # bounded retries are managed here
        )

    async def chat(self, *, system: str, messages: list[Message]) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system}, *messages],
                temperature=0.7,
            )
        except APIError as exc:
            raise AIProviderError(f"chat completion failed: {exc}") from exc
        return response.choices[0].message.content or ""

    async def chat_structured(
        self, *, system: str, messages: list[Message], schema: type[T]
    ) -> T:
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "schema": schema.model_json_schema(),
            },
        }
        last_error: Exception | None = None
        for _ in range(self._max_attempts):
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "system", "content": system}, *messages],
                    temperature=0,
                    response_format=response_format,
                )
            except APIError as exc:
                raise AIProviderError(
                    f"structured completion failed: {exc}"
                ) from exc
            content = response.choices[0].message.content
            try:
                data = json.loads(content) if content else {}
                return schema.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = exc
        raise AIOutputValidationError(
            f"model output failed schema validation after "
            f"{self._max_attempts} attempts"
        ) from last_error

    async def embed(self, *, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = await self._client.embeddings.create(
                model=self._embedding_model,
                input=texts,
            )
        except APIError as exc:
            raise AIProviderError(f"embedding failed: {exc}") from exc
        return [list(item.embedding) for item in response.data]
