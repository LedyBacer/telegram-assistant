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
import logging
import re
from typing import Protocol, TypeVar, runtime_checkable

from openai import APIError, APITimeoutError, AsyncOpenAI, Timeout
from pydantic import BaseModel, ValidationError

logger = logging.getLogger("assistant.ai")

T = TypeVar("T", bound=BaseModel)

# Appended to the system prompt for every structured call so the model emits
# a bare JSON object even when its own defaults add prose around it.
JSON_OUTPUT_INSTRUCTION = (
    "\n\nOutput format: reply with ONLY a single valid JSON object that "
    "matches the required schema. No markdown, no code fences, no comments, "
    "no text before or after the JSON."
)

# One chat turn: {"role": "system"|"user"|"assistant", "content": str}.
Message = dict[str, str]

# E5 retrieval prefixes (multilingual-e5-small). Stored document chunks are
# embedded as "passage: <text>" and search queries as "query: <text>". The
# prefixes are applied only to the text sent to the embedding model; the
# original chunk/query text is never modified.
E5_DOCUMENT_PREFIX = "passage: "
E5_QUERY_PREFIX = "query: "


def _balanced_object_span(text: str, start: int) -> int | None:
    """End index (inclusive) of the brace-balanced object opened at ``start``.

    Tracks double-quoted strings and escapes so braces inside string values
    do not break the balance. Returns None when the object is unbalanced.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def extract_json_object(content: str) -> dict:
    """Extract the first JSON object from raw model content.

    Tolerates real Qwen/llama.cpp output shapes: a bare object, an object
    wrapped in ```json fences, a thinking preamble in front of the object,
    and trailing prose after it. Raises ``ValueError`` when no object can be
    parsed (the caller decides whether to retry).
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("empty model response")
    fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    candidates: list[str] = []
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text)
    balanced: list[str] = []
    start = text.find("{")
    while start != -1:
        end = _balanced_object_span(text, start)
        if end is None:
            start = text.find("{", start + 1)
        else:
            balanced.append(text[start : end + 1])
            start = text.find("{", end + 1)  # nested objects are not separate candidates
    # Models often echo the requested format (a small valid JSON example) in
    # a thinking preamble before the real answer, so prefer the LAST
    # balanced object when several parse.
    candidates.extend(reversed(balanced))
    for span in candidates:
        try:
            data = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("no JSON object in model response")


def _redact_for_log(exc: APIError) -> str:
    """Provider error text for logs: keeps the diagnostic, drops secrets."""
    text = str(exc)
    text = re.sub(r"(?i)(api[_-]?key|authorization|bearer)[=:]\s*\S+", r"\1=<redacted>", text)
    return text[:1000]


class AIProviderError(RuntimeError):
    """The model provider could not be reached or returned an API error."""


class AITimeoutError(AIProviderError):
    """The provider did not finish within the configured chat timeout.

    Distinct from :class:`AIOutputValidationError`: a full inference timeout
    is not a model misbehaviour that a second equally long inference would
    fix, so callers do not retry it.
    """


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
        timeout: float = 180.0,
        thinking_enabled: bool = True,
    ) -> None:
        self._model = model
        self._max_attempts = max_attempts
        self._timeout = timeout
        self._thinking_enabled = thinking_enabled
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            # A non-streaming completion is a single read phase, so the
            # configured value bounds the whole generation; connect/write/
            # pool stay short and fail fast on an unhealthy network.
            timeout=Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0),
            max_retries=0,  # bounded retries are managed here
        )

    def _chat_options(self) -> dict:
        """Provider request options shared by every chat completion call.

        ``chat_template_kwargs`` is the documented llama.cpp OpenAI-compatible
        extension for the Jinja chat template; ``enable_thinking`` toggles
        Qwen reasoning. It is sent explicitly in both directions so the mode
        never relies on a server-side default. Embeddings never use it.
        """
        return {
            "extra_body": {
                "chat_template_kwargs": {"enable_thinking": self._thinking_enabled}
            }
        }

    async def chat(self, *, system: str, messages: list[Message]) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system}, *messages],
                temperature=0.7,
                store=False,
                **self._chat_options(),
            )
        except APITimeoutError as exc:
            logger.warning(
                "chat completion timed out after %.0f s (model=%s): %s",
                self._timeout,
                self._model,
                _redact_for_log(exc),
            )
            raise AITimeoutError(
                f"chat completion timed out after {self._timeout:g} s"
            ) from exc
        except APIError as exc:
            raise AIProviderError(f"chat completion failed: {exc}") from exc
        return response.choices[0].message.content or ""

    async def chat_structured(
        self, *, system: str, messages: list[Message], schema: type[T]
    ) -> T:
        """Structured completion compatible with llama.cpp ``/v1``.

        Does NOT send OpenAI-only ``response_format=json_schema`` (llama.cpp
        rejects/ignores it, which breaks extraction on real deployments): the
        JSON contract is carried in the system prompt, and the JSON object is
        extracted from plain content (Qwen-style thinking preambles, code
        fences, and trailing prose all tolerated) before Pydantic validation.
        """
        system_with_json = system + JSON_OUTPUT_INSTRUCTION
        last_error: Exception | None = None
        conversation: list[Message] = list(messages)
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "system", "content": system_with_json}, *conversation],
                    temperature=0,
                    store=False,
                    **self._chat_options(),
                )
            except APITimeoutError as exc:
                # A full inference timeout is not a malformed response:
                # immediately repeating an equally long inference would only
                # double the user wait, so it fails cleanly here instead of
                # consuming the remaining attempts.
                logger.warning(
                    "structured completion timed out after %.0f s "
                    "(model=%s schema=%s attempt=%d/%d input_chars=%d); "
                    "not retrying a full inference: %s",
                    self._timeout,
                    self._model,
                    schema.__name__,
                    attempt,
                    self._max_attempts,
                    sum(len(m["content"]) for m in conversation),
                    _redact_for_log(exc),
                )
                raise AITimeoutError(
                    f"structured completion timed out after {self._timeout:g} s"
                ) from exc
            except APIError as exc:
                logger.warning(
                    "structured completion API error (model=%s schema=%s "
                    "attempt=%d/%d input_chars=%d): %s",
                    self._model,
                    schema.__name__,
                    attempt,
                    self._max_attempts,
                    sum(len(m["content"]) for m in conversation),
                    _redact_for_log(exc),
                )
                raise AIProviderError(
                    f"structured completion failed: {exc}"
                ) from exc
            content = response.choices[0].message.content or ""
            try:
                data = extract_json_object(content)
                return schema.model_validate(data)
            except (ValueError, ValidationError) as exc:
                last_error = exc
                logger.info(
                    "structured output rejected (model=%s schema=%s "
                    "attempt=%d/%d content_chars=%d): %s",
                    self._model,
                    schema.__name__,
                    attempt,
                    self._max_attempts,
                    len(content),
                    type(exc).__name__,
                )
                if attempt < self._max_attempts:
                    # Corrective feedback so the next attempt can self-repair.
                    conversation = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {
                            "role":
                            "user",
                            "content": (
                                "Your previous reply was not a valid JSON object "
                                f"matching the required schema ({exc}). Reply again "
                                "with ONLY the corrected JSON object."
                            ),
                        },
                    ]
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
