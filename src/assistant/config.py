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

    # Test-only Mini App auth (E2E). Enabled ONLY via the ASSISTANT_TEST_AUTH
    # environment variable set by the Playwright test harness; production and
    # development never set it, so the bypass cannot activate from a normal
    # HTTP request. When enabled, the auth dependency is replaced at startup
    # with a deterministic test user (no initData verification).
    test_auth_enabled: bool = Field(
        default=False,
        validation_alias="ASSISTANT_TEST_AUTH",
    )
    test_auth_user_id: int = 999999
    test_auth_first_name: str = "Test"
    test_auth_last_name: str = "User"
    test_auth_username: str = "e2e"

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
    # How long (seconds) the application waits for a chat/generation
    # completion from the provider. llama.cpp reasoning can exceed the
    # OpenAI client's implicit 60 s default, so this is explicit.
    chat_timeout_seconds: float = Field(default=180.0, ge=1, le=3600)
    # Qwen thinking (reasoning) mode for the chat provider: sent explicitly
    # to the llama.cpp server as chat_template_kwargs.enable_thinking.
    # Applies to chat + structured generation only, never to embeddings.
    #
    # Default is FAST / NO-THINK. Reasoning is an OPTIONAL profile, opt-in
    # per deployment via CHAT_THINKING_ENABLED / CHAT_REASONING_EFFORT, so a
    # production llama.cpp deployment does not enable thinking by surprise.
    # The app must work fully without it.
    chat_thinking_enabled: bool = False
    chat_reasoning_effort: str | None = None  # "low" | "medium" | "high" | None
    embedding_api_key: str | None = None
    embedding_base_url: str | None = None
    embedding_model: str = "multilingual-e5-small"
    embedding_dimensions: int = Field(default=384, ge=1)

    @model_validator(mode="after")
    def _resolve_provider_credentials(self) -> Settings:
        # Fill provider-specific variables from the legacy single-provider
        # ones. Chat is REQUIRED; embeddings are OPTIONAL — a deployment
        # without an embedding provider runs chat-only with lexical-only
        # document search (V3 P42).
        for name in ("chat_api_key", "embedding_api_key"):
            if getattr(self, name) is None:
                setattr(self, name, self.openai_api_key)
        for name in ("chat_base_url", "embedding_base_url"):
            if getattr(self, name) is None:
                setattr(self, name, self.openai_base_url)
        if not self.chat_api_key:
            raise ValueError(
                "AI provider credentials are not configured: set "
                "CHAT_API_KEY (or the legacy OPENAI_API_KEY)"
            )
        return self

    @property
    def embedding_configured(self) -> bool:
        """True when an embedding provider is configured (V3 P42)."""
        return bool(self.embedding_api_key)

    # File ingestion
    max_upload_size_bytes: int = Field(
        default=20 * 1024 * 1024, ge=1, le=500 * 1024 * 1024
    )
    chunk_size: int = Field(default=1000, ge=50, le=20_000)
    chunk_overlap: int = Field(default=150, ge=0, le=10_000)
    file_storage_dir: str = "storage/files"
    embedding_batch_size: int = Field(default=64, ge=1, le=512)

    # Ingestion resource safety (SPEC §21): hostile inputs (highly compressed
    # PDF/DOCX, huge text) must not cause unbounded CPU/memory work. The
    # extracted text of one file is capped in characters, the chunk count —
    # and therefore the number of embedding batches and chunk rows written —
    # is capped per file, and PDFs with more pages than ``max_pdf_pages``
    # fail visibly instead of being parsed in full.
    max_extracted_text_chars: int = Field(default=2_000_000, ge=1_000, le=50_000_000)
    max_chunks_per_file: int = Field(default=2_000, ge=1, le=20_000)
    max_pdf_pages: int = Field(default=500, ge=1, le=10_000)

    # Retrieval relevance policy (SPEC §13, §17): a vector candidate is only
    # "meaningfully relevant" when its cosine distance to the query is within
    # this bound (0 = identical, 1 = orthogonal, 2 = opposite). Candidates
    # beyond it are dropped, so a search that is semantically off-topic does
    # not return its nearest-but-unrelated chunks. Lexical (keyword-overlap)
    # candidates are always kept regardless of this bound.
    retrieval_max_distance: float = Field(default=0.9, ge=0.0, le=2.0)

    # Adjacent-chunk merging (SPEC §13): retrieved chunks from the same file
    # whose positions are within this many steps of each other are merged into
    # one bounded excerpt (1 = strictly consecutive; a larger value bridges a
    # small gap of un-retrieved chunks).
    retrieval_merge_gap: int = Field(default=1, ge=0, le=50)

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
