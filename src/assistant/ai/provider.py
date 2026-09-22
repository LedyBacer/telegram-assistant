"""OpenAI-compatible AI providers behind a replaceable protocol (SPEC §2, §27).

Chat/generation and embeddings are served by *independent* OpenAI-compatible
clients, each with its own base URL, API key, and model (see ``Settings``),
so the two capabilities may live on separate servers (e.g. two llama.cpp
instances) without assuming a shared endpoint.

The SDK clients are the only OpenAI-specific code in the package. Transient
and persistent provider failures are surfaced as narrow, meaningful error
classes instead of leaking SDK exceptions to callers.
"""

from __future__ import annotations

import json
from typing import Protocol, TypeVar, runtime_checkable

from openai import APIError, AsyncOpenAI
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# One chat turn: {"role": "system"|"user"|"assistant", "content": str}.
Message = dict[str, str]

# E5 retrieval prefixes (multilingual-e5-small). Stored document chunks are
# embedded as "passage: <text>" and search queries as "query: <text>". The
# prefixes are applied only to the text sent to the embedding model; the
# original chunk/query text is never modified.
E5_DOCUMENT_PREFIX = "passage: "
E5_QUERY_PREFIX = "query: "


class AIProviderError(RuntimeError):
    """The model provider could not be reached or returned an API error."""


class AIOutputValidationError(AIProviderError):
    """The model repeatedly produced output that failed schema validation."""


class EmbeddingDimensionError(AIProviderError):
    """The embedding server returned a vector with an unexpected dimension."""


@runtime_checkable
class AIProvider(Protocol):
    """Interface for chat completion and embedding providers."""

    async def chat(self, *, system: str, messages: list[Message]) -> str:
        """Plain conversational completion."""
        ...

    async def chat_structured(
        self, *, system: str, messages: list[Message], schema: type[T]
    ) -> T:
        """Completion whose content is validated against a Pydantic schema."""
        ...

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        """Embed stored document chunks (one vector per input, same order).

        The E5 document prefix is applied here, not by callers.
        """
        ...

    async def embed_query(self, *, query: str) -> list[float]:
        """Embed one user search query. The E5 query prefix is applied here."""
        ...


class OpenAIChatProvider:
    """Chat client for one OpenAI-compatible endpoint (llama.cpp ``/v1``)."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        model: str = "qwen3.5-9b-64k",
        max_attempts: int = 2,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
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
                store=False,
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
                    store=False,
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


class OpenAIEmbeddingProvider:
    """Embeddings client for one OpenAI-compatible endpoint
    (llama.cpp ``/v1/embeddings``).

    Applies the E5 prefixes expected by the model and validates that every
    returned vector has the configured dimension.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        model: str = "multilingual-e5-small",
        dimensions: int = 384,
        timeout: float = 60.0,
    ) -> None:
        self._model = model
        self._dimensions = dimensions
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        return await self._embed([f"{E5_DOCUMENT_PREFIX}{text}" for text in texts])

    async def embed_query(self, *, query: str) -> list[float]:
        (vector,) = await self._embed([f"{E5_QUERY_PREFIX}{query}"])
        return vector

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            # Only the OpenAI-compatible request shape (model + input);
            # llama.cpp /v1/embeddings does not support OpenAI-specific
            # options such as `dimensions`.
            response = await self._client.embeddings.create(
                model=self._model,
                input=texts,
            )
        except APIError as exc:
            raise AIProviderError(f"embedding failed: {exc}") from exc
        vectors: list[list[float]] = []
        for item in response.data:
            vector = [float(value) for value in item.embedding]
            if len(vector) != self._dimensions:
                raise EmbeddingDimensionError(
                    f"embedding dimension mismatch: expected "
                    f"{self._dimensions}, got {len(vector)}"
                )
            vectors.append(vector)
        return vectors


class OpenAICompatibleProvider:
    """Composite ``AIProvider``: chat and embedding calls are routed to
    independent OpenAI-compatible clients (see ``build_ai_provider``)."""

    def __init__(
        self,
        *,
        chat: OpenAIChatProvider,
        embedding: OpenAIEmbeddingProvider,
    ) -> None:
        self._chat_provider = chat
        self._embedding_provider = embedding

    async def chat(self, *, system: str, messages: list[Message]) -> str:
        return await self._chat_provider.chat(system=system, messages=messages)

    async def chat_structured(
        self, *, system: str, messages: list[Message], schema: type[T]
    ) -> T:
        return await self._chat_provider.chat_structured(
            system=system, messages=messages, schema=schema
        )

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        return await self._embedding_provider.embed_documents(texts=texts)

    async def embed_query(self, *, query: str) -> list[float]:
        return await self._embedding_provider.embed_query(query=query)
