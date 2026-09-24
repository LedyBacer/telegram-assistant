"""AI provider abstraction (SPEC §2, §15, §30).

External model calls go through the `AIProvider` protocol so the concrete
OpenAI-compatible clients can be replaced (or faked in tests) without
touching call sites. Chat/generation and embeddings use *independent*
clients configured from `CHAT_*` / `EMBEDDING_*` settings (with a fallback
to the legacy `OPENAI_*` variables), so the two capabilities may be served
by separate servers.

The provider is built lazily: importing this package never reads API keys,
so the test suite runs without credentials.
"""

from __future__ import annotations

from functools import lru_cache

from assistant.ai.provider import (
    E5_DOCUMENT_PREFIX,
    E5_QUERY_PREFIX,
    AIOutputValidationError,
    AIProvider,
    AIProviderError,
    AITimeoutError,
    EmbeddingDimensionError,
    OpenAIChatProvider,
    OpenAICompatibleProvider,
    OpenAIEmbeddingProvider,
    extract_json_object,
)
from assistant.ai.schemas import AITaskDraft
from assistant.config import Settings, get_settings

__all__ = [
    "AITaskDraft",
    "AITimeoutError",
    "AIOutputValidationError",
    "AIProvider",
    "AIProviderError",
    "E5_DOCUMENT_PREFIX",
    "E5_QUERY_PREFIX",
    "EmbeddingDimensionError",
    "OpenAIChatProvider",
    "OpenAICompatibleProvider",
    "OpenAIEmbeddingProvider",
    "build_ai_provider",
    "extract_json_object",
    "get_ai_provider",
]


def build_ai_provider(settings: Settings) -> AIProvider:
    """Create a provider with independent chat and embedding clients.

    The embedding client is optional (V3 P42): without an embedding API key
    the composite runs chat-only — retrieval degrades to lexical-only and
    document uploads are rejected with a visible reason — while chat,
    actions, and every non-embedding feature are unaffected.
    """
    return OpenAICompatibleProvider(
        chat=OpenAIChatProvider(
            api_key=settings.chat_api_key,
            base_url=settings.chat_base_url,
            model=settings.chat_model,
            timeout=settings.chat_timeout_seconds,
            thinking_enabled=settings.chat_thinking_enabled,
            reasoning_effort=settings.chat_reasoning_effort,
        ),
        embedding=(
            OpenAIEmbeddingProvider(
                api_key=settings.embedding_api_key,
                base_url=settings.embedding_base_url,
                model=settings.embedding_model,
                dimensions=settings.embedding_dimensions,
            )
            if settings.embedding_configured
            else None
        ),
    )


@lru_cache
def get_ai_provider() -> AIProvider:
    """Process-wide provider instance built from application settings."""
    return build_ai_provider(get_settings())
