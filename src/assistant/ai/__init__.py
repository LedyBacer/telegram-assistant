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
    EmbeddingDimensionError,
    OpenAIChatProvider,
    OpenAICompatibleProvider,
    OpenAIEmbeddingProvider,
)
from assistant.ai.schemas import AITaskDraft
from assistant.config import Settings, get_settings

__all__ = [
    "AITaskDraft",
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
    "get_ai_provider",
]


def build_ai_provider(settings: Settings) -> AIProvider:
    """Create a provider with independent chat and embedding clients."""
    return OpenAICompatibleProvider(
        chat=OpenAIChatProvider(
            api_key=settings.chat_api_key,
            base_url=settings.chat_base_url,
            model=settings.chat_model,
        ),
        embedding=OpenAIEmbeddingProvider(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        ),
    )


@lru_cache
def get_ai_provider() -> AIProvider:
    """Process-wide provider instance built from application settings."""
    return build_ai_provider(get_settings())
