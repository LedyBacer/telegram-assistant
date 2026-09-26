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
import time
from typing import Protocol, TypeVar, runtime_checkable

from openai import APIError, APITimeoutError, AsyncOpenAI, Timeout
from pydantic import BaseModel, ValidationError

from assistant.ai.structured_events import count_structured_event

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
        thinking_enabled: bool = False,
        thinking_budget_tokens: int | None = None,
        reasoning_effort: str | None = None,
        sampling_profile: str = "auto",
        structured_sampling: str = "precise",
    ) -> None:
        self._model = model
        self._max_attempts = max_attempts
        self._timeout = timeout
        self._thinking_enabled = thinking_enabled
        self._thinking_budget_tokens = thinking_budget_tokens
        self._reasoning_effort = reasoning_effort
        self._sampling_profile = sampling_profile
        self._structured_sampling = structured_sampling
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            # A non-streaming completion is a single read phase, so the
            # configured value bounds the whole generation; connect/write/
            # pool stay short and fail fast on an unhealthy network.
            timeout=Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0),
            max_retries=0,  # bounded retries are managed here
        )

    def _resolve_sampling_profile(self) -> str:
        """Return the effective sampling profile (V5.4 §4).

        An explicit profile is used verbatim. "auto" infers the model-card
        family from the served alias so the reasoning family applies to both
        ``qwen3.5*`` and the ``ornith*`` aliases that serve the same model
        family, while unknown models fall back to the legacy defaults.
        """
        profile = self._sampling_profile
        if profile == "auto":
            model = self._model.lower()
            if "qwen3.5" in model or "qwen3_5" in model or "ornith" in model:
                return "qwen35_reasoning"
            return "legacy"
        return profile

    def _request_options(self, *, structured: bool) -> dict:
        """Chat-template + sampling options for the current model/profile.

        The sampling family is chosen by the resolved sampling profile, not
        by thinking mode, so the production A/B study (thinking on vs off,
        V5.4 §25) toggles only ``enable_thinking`` while sampling stays
        constant.

        The qwen35_reasoning profile sends the model-card sampling: general
        chat uses temp=1.0 / presence_penalty=1.5; structured JSON uses the
        precise profile temp=0.6 / presence_penalty=0.0 (or the general
        family when ``structured_sampling == "general"``). Both use
        top_p=.95, top_k=20, min_p=0 and repeat_penalty=1.0. The legacy
        profile keeps the historical greedy structured / 0.7 general path.
        """
        template_kwargs: dict = {"enable_thinking": self._thinking_enabled}
        if self._thinking_enabled and self._reasoning_effort:
            template_kwargs["reasoning_effort"] = self._reasoning_effort

        extra_body: dict = {"chat_template_kwargs": template_kwargs}
        if self._thinking_enabled and self._thinking_budget_tokens is not None:
            extra_body["thinking_budget_tokens"] = self._thinking_budget_tokens

        if self._resolve_sampling_profile() == "qwen35_reasoning":
            extra_body.update({"top_k": 20, "min_p": 0.0, "repeat_penalty": 1.0})
            if structured and self._structured_sampling == "general":
                temperature, presence = 1.0, 1.5
            elif structured:
                temperature, presence = 0.6, 0.0
            else:
                temperature, presence = 1.0, 1.5
            return {
                "temperature": temperature,
                "top_p": 0.95,
                "presence_penalty": presence,
                "extra_body": extra_body,
            }

        return {
            "temperature": 0 if structured else 0.7,
            "extra_body": extra_body,
        }

    async def chat(self, *, system: str, messages: list[Message]) -> str:
        started = time.monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "system", "content": system}, *messages],
                store=False,
                **self._request_options(structured=False),
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
        logger.info(
            "chat ok (model=%s duration_s=%.2f)",
            self._model,
            time.monotonic() - started,
        )
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
        started = time.monotonic()
        last_error: Exception | None = None
        conversation: list[Message] = list(messages)
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[{"role": "system", "content": system_with_json}, *conversation],
                    store=False,
                    **self._request_options(structured=True),
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
                result = schema.model_validate(data)
                logger.info(
                    "structured ok (model=%s schema=%s attempt=%d/%d duration_s=%.2f)",
                    self._model,
                    schema.__name__,
                    attempt,
                    self._max_attempts,
                    time.monotonic() - started,
                )
                return result
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
                    count_structured_event(
                        "structured_validation_retry",
                        schema=schema.__name__,
                        attempt=attempt,
                    )
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
        count_structured_event(
            "structured_validation_final_failure",
            schema=schema.__name__,
            attempts=self._max_attempts,
        )
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
        started = time.monotonic()
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
        logger.info(
            "embed ok (model=%s n=%d duration_s=%.2f)",
            self._model,
            len(texts),
            time.monotonic() - started,
        )
        return vectors


class OpenAICompatibleProvider:
    """Composite ``AIProvider``: chat and embedding calls are routed to
    independent OpenAI-compatible clients (see ``build_ai_provider``).

    The embedding client is optional (V3 P42): a chat-only deployment passes
    ``embedding=None``, reports ``embedding_configured == False``, and its
    embedding methods fail fast with :class:`AIProviderError` — retrieval
    callers catch that and degrade to lexical-only, while chat is unaffected.
    """

    def __init__(
        self,
        *,
        chat: OpenAIChatProvider,
        embedding: OpenAIEmbeddingProvider | None = None,
    ) -> None:
        self._chat_provider = chat
        self._embedding_provider = embedding

    @property
    def embedding_configured(self) -> bool:
        """True when an embedding client is attached."""
        return self._embedding_provider is not None

    def _require_embedding(self) -> OpenAIEmbeddingProvider:
        if self._embedding_provider is None:
            raise AIProviderError(
                "embedding provider is not configured (set EMBEDDING_API_KEY "
                "or the legacy OPENAI_API_KEY)"
            )
        return self._embedding_provider

    async def chat(self, *, system: str, messages: list[Message]) -> str:
        return await self._chat_provider.chat(system=system, messages=messages)

    async def chat_structured(
        self, *, system: str, messages: list[Message], schema: type[T]
    ) -> T:
        return await self._chat_provider.chat_structured(
            system=system, messages=messages, schema=schema
        )

    async def embed_documents(self, *, texts: list[str]) -> list[list[float]]:
        return await self._require_embedding().embed_documents(texts=texts)

    async def embed_query(self, *, query: str) -> list[float]:
        return await self._require_embedding().embed_query(query=query)
