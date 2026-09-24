"""Structured logging shared by all runtime processes (SPEC §23).

Every record is emitted as a **single-line JSON object** carrying the
component (the logger name, e.g. ``assistant.worker``), the timestamp, level,
message, exception details, and any per-request/job **context** — user, job,
file, and request/correlation identifiers — so an operator can correlate a
worker failure, an AI call, or an API request across log lines.

Context is set per unit of work by the entry points (API request middleware,
worker job runner, bot update middleware) via :func:`log_context` and flows
into every record logged within that unit through a ``contextvars`` variable
and :class:`ContextFilter`.

Secrets are **never logged**: a redaction pass replaces the configured bot
token and API keys in every message and exception string, so even an
accidental interpolation cannot leak a credential.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# Per-async-context log context. Set by request/job/update middleware so every
# record logged within the unit carries the same correlation fields.
_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("log_context", default=None)

# Fields emitted at the top level when present in the context.
_CONTEXT_KEYS = (
    "request_id",
    "user_id",
    "chat_id",
    "job_id",
    "job_type",
    "file_id",
)


def current_context() -> dict[str, Any]:
    """Return the active log context as a fresh dict (empty if none set)."""
    return dict(_CONTEXT.get() or {})


def bind_context(**fields: Any) -> None:
    """Merge non-None ``fields`` into the active log context (additive)."""
    merged = current_context()
    merged.update({k: v for k, v in fields.items() if v is not None})
    _CONTEXT.set(merged)


def clear_context() -> None:
    """Discard the active log context."""
    _CONTEXT.set(None)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Temporarily merge non-None ``fields`` into the log context.

    The previous context is restored on exit, so nested units (e.g. a job
    running inside a request) keep their outer correlation fields.
    """
    merged = current_context()
    merged.update({k: v for k, v in fields.items() if v is not None})
    token = _CONTEXT.set(merged)
    try:
        yield
    finally:
        _CONTEXT.reset(token)


class ContextFilter(logging.Filter):
    """Attach the active log context to each record for the formatter."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.context = current_context()
        return True


class _SecretRedactor:
    """Replace configured secret values in text before it is logged.

    Defense in depth: a credential must never reach the log, even if a caller
    accidentally interpolates one. Reads the live settings each call (cached
    until the secret set changes) so reconfigured deployments and tests are
    respected. Never raises — logging must stay robust.
    """

    def __init__(self) -> None:
        self._cache_key: str | None = None
        self._secrets: tuple[str, ...] = ()

    def _reload(self) -> None:
        try:
            from assistant.config import get_settings

            settings = get_settings()
        except Exception:
            # Settings unavailable (e.g. very early bootstrap): redact nothing
            # but never break the logging path.
            self._secrets = ()
            self._cache_key = ""
            return
        raw = (
            settings.telegram_bot_token,
            settings.openai_api_key,
            settings.chat_api_key,
            settings.embedding_api_key,
        )
        secrets = tuple(value for value in raw if value)
        key = repr(secrets)
        if key != self._cache_key:
            self._secrets = secrets
            self._cache_key = key

    def redact(self, text: str) -> str:
        if not text:
            return text
        self._reload()
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, "[REDACTED]")
        return text


_REDACTOR = _SecretRedactor()


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per record: context, redacted message, exception."""

    def format(self, record: logging.LogRecord) -> str:
        context = getattr(record, "context", None) or {}
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": _REDACTOR.redact(record.getMessage()),
        }
        # Known correlation fields first, in a stable order; then any extra
        # context keys so nothing is silently dropped.
        for key in _CONTEXT_KEYS:
            if key in context:
                payload[key] = context[key]
        for key, value in context.items():
            payload.setdefault(key, value)
        if record.exc_info is not None:
            etype, value, tb = record.exc_info
            payload["exc"] = _REDACTOR.redact(self.formatException((etype, value, tb)))
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger with structured JSON output on stdout.

    Idempotent: safe to call once per process; reconfigures on repeated calls.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
