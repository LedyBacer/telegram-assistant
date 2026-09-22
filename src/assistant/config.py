"""Application settings loaded from the environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Core
    database_url: str = Field(description="SQLAlchemy async URL (asyncpg)")
    public_base_url: str = Field(description="Public base URL used in bot replies and the Mini App")
    app_timezone: str = "UTC"
    log_level: str = "INFO"

    # Telegram
    telegram_bot_token: str
    miniapp_auth_max_age_seconds: int = 900
    miniapp_dir: str = "miniapp"

    # AI providers (OpenAI-compatible).
    #
    # Chat/generation and embeddings are configured independently: the two
    # capabilities may be served by separate servers (e.g. two llama.cpp
    # instances), each with its own base URL, API key, and model.
    #
    # Legacy fallback: when the provider-specific variables are absent,
    # OPENAI_API_KEY / OPENAI_BASE_URL are used for both providers.
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    chat_api_key: str | None = None
    chat_base_url: str | None = None
    chat_model: str = "qwen3.5-9b-64k"
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str = "multilingual-e5-small"
    embedding_dimensions: int = Field(default=384, ge=1)

    @model_validator(mode="after")
    def _resolve_provider_credentials(self) -> Settings:
        # Fill provider-specific variables from the legacy single-provider
        # ones, then require that both providers end up with an API key.
        for name in ("chat_api_key", "embedding_api_key"):
            if getattr(self, name) is None:
                setattr(self, name, self.openai_api_key)
        for name in ("chat_base_url", "embedding_base_url"):
            if getattr(self, name) is None:
                setattr(self, name, self.openai_base_url)
        missing = [name for name in ("chat_api_key", "embedding_api_key") if not getattr(self, name)]
        if missing:
            raise ValueError(
                "AI provider credentials are not configured: set "
                + ", ".join(missing)
                + " (or the legacy OPENAI_API_KEY)"
            )
        return self

    # File ingestion
    max_upload_size_bytes: int = 20 * 1024 * 1024
    chunk_size: int = 1000
    chunk_overlap: int = 150
    file_storage_dir: str = "storage/files"
    embedding_batch_size: int = 64

    # Contextual chat
    chat_history_messages: int = 10

    # Durable job worker
    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 10
    job_max_attempts: int = 3

    # Morning digest scheduling
    digest_schedule_interval_seconds: float = 30.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
