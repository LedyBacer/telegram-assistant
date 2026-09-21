"""AI provider abstraction (SPEC §2, §15, §30).

External model calls go through the `AIProvider` protocol so the concrete
OpenAI-compatible client can be replaced (or faked in tests) without touching
call sites. The provider is built lazily: importing this package never reads
API keys, so the test suite runs without credentials.
"""

from __future__ import annotations

from functools import lru_cache

from assistant.ai.provider import (
    AIOutputValidationError,
    AIProvider,
    AIProviderError,
    OpenAICompatibleProvider,
)
from assistant.ai.schemas import AITaskDraft
from assistant.config import Settings, get_settings

__all__ = [
    "AITaskDraft",
    "AIOutputValidationError",
    "AIProvider",
    "AIProviderError",
    "OpenAICompatibleProvider",
    "build_ai_provider",
    "get_ai_provider",
]


def build_ai_provider(settings: Settings) -> AIProvider:
    """Create a provider for the given settings (OpenAI-compatible endpoint)."""
    return OpenAICompatibleProvider(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        chat_model=settings.chat_model,
    )


@lru_cache
def get_ai_provider() -> AIProvider:
    """Process-wide provider instance built from application settings."""
    return build_ai_provider(get_settings())
