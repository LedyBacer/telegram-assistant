"""Application settings loaded from the environment."""

from functools import lru_cache

from pydantic import Field
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

    # AI providers (OpenAI-compatible)
    openai_api_key: str
    openai_base_url: str | None = None
    chat_model: str = "gpt-4o-mini"
    embedding_model: str = "text-embedding-3-small"

    # File ingestion
    max_upload_size_bytes: int = 20 * 1024 * 1024
    chunk_size: int = 1000
    chunk_overlap: int = 150
    file_storage_dir: str = "storage/files"
    embedding_batch_size: int = 64

    # Durable job worker
    worker_poll_interval_seconds: float = 1.0
    worker_batch_size: int = 10
    job_max_attempts: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
