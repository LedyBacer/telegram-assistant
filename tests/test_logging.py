"""P46 structured/contextual logging (SPEC §23).

Covers: JSON record shape, per-unit context propagation (request/job/user ids),
secret redaction (message + exception), exception detail capture, nesting, and
that ``setup_logging`` wires a JSON handler onto the root logger.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import pytest

os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://assistant:assistant@localhost:5432/assistant",
    ),
)
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")
os.environ.setdefault("OPENAI_API_KEY", "test-key")

from assistant.config import get_settings  # noqa: E402
from assistant.logging import (  # noqa: E402
    JsonFormatter,
    current_context,
    log_context,
    setup_logging,
)


def _record(
    msg: str,
    *,
    level: int = logging.INFO,
    logger_name: str = "assistant.test",
    args: tuple = (),
    exc_info: Any = None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=logger_name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )


def _emit(record: logging.LogRecord) -> dict[str, Any]:
    """Format ``record`` exactly as the runtime handler would.

    The production handler attaches a :class:`ContextFilter` that stamps the
    active log context onto the record; replicate that, then JSON-decode.
    """
    record.context = current_context()
    return json.loads(JsonFormatter().format(record))


def test_json_record_has_core_fields() -> None:
    out = _emit(_record("hello %s", args=("world",)))
    assert out["logger"] == "assistant.test"
    assert out["level"] == "INFO"
    assert out["msg"] == "hello world"
    assert out["ts"].endswith("+00:00")


def test_context_fields_appear_in_record() -> None:
    with log_context(request_id="req-1", user_id=42, job_id=7, file_id="f9"):
        out = _emit(_record("did a thing"))
    assert out["request_id"] == "req-1"
    assert out["user_id"] == 42
    assert out["job_id"] == 7
    assert out["file_id"] == "f9"


def test_nested_context_restores_outer_fields() -> None:
    with log_context(request_id="outer-req", user_id=1):
        with log_context(job_id=99):
            inner = current_context()
        outer = current_context()
    assert inner["job_id"] == 99
    assert inner["request_id"] == "outer-req"
    assert "job_id" not in outer
    assert outer["request_id"] == "outer-req"


def test_secret_redaction_in_message(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "S3CR3T-BOT-TOKEN-xyz")
    monkeypatch.setattr(settings, "chat_api_key", "S3CR3T-KEY-abc")

    out = _emit(_record("token=S3CR3T-BOT-TOKEN-xyz key=S3CR3T-KEY-abc"))
    assert "S3CR3T-BOT-TOKEN-xyz" not in out["msg"]
    assert "S3CR3T-KEY-abc" not in out["msg"]
    assert out["msg"].count("[REDACTED]") == 2


def test_secret_redaction_in_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "chat_api_key", "TOPSECRET-42")

    try:
        raise ValueError("auth failed for TOPSECRET-42")
    except ValueError as exc:
        exc_info = (type(exc), exc, exc.__traceback__)
        out = _emit(_record("call blew up", level=logging.ERROR, exc_info=exc_info))
    assert out["exc"] is not None
    assert "TOPSECRET-42" not in out["exc"]
    assert "[REDACTED]" in out["exc"]
    # The diagnostic (exception type) is preserved.
    assert "ValueError" in out["exc"]


def test_exception_details_present() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        exc_info = (type(exc), exc, exc.__traceback__)
        out = _emit(_record("it failed", level=logging.ERROR, exc_info=exc_info))
    assert "RuntimeError" in out["exc"]
    assert "boom" in out["exc"]


def test_setup_logging_wires_json_handler() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    try:
        setup_logging("INFO")
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler.formatter, JsonFormatter)
        # A record emitted through the wired handler must serialize to JSON.
        record = _record("ping")
        record.context = current_context()
        assert json.loads(handler.formatter.format(record))["msg"] == "ping"
        assert root.level == logging.INFO
    finally:
        root.handlers.clear()
        root.handlers.extend(original_handlers)
        root.setLevel(original_level)
