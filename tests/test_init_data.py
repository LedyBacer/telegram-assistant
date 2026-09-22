"""Unit tests for Telegram Mini App initData HMAC verification (SPEC §19).

Valid payloads are generated here with the same algorithm Telegram uses, so
no network access or real token is required.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable

import pytest

from assistant.api.auth import InitDataError, verify_init_data

BOT_TOKEN = "123456:TEST-TOKEN"
MAX_AGE = 900


def _sign(params: dict[str, str]) -> str:
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    return hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()


def make_init_data(
    *,
    user: dict | None = None,
    raw_user: str | None = None,
    auth_age_seconds: int = 0,
    token: str = BOT_TOKEN,
    tamper: Callable[[dict[str, str]], None] | None = None,
    extra: dict[str, str] | None = None,
    omit: set[str] | None = None,
) -> str:
    params: dict[str, str] = {
        "user": raw_user if raw_user is not None else json.dumps(
            user or {"id": 777, "first_name": "T"}
        ),
        "auth_date": str(int(time.time()) - auth_age_seconds),
        "query_id": "AAH-tampered-query-id",
    }
    if extra:
        params.update(extra)
    for key in omit or set():
        params.pop(key, None)
    params["hash"] = _sign(params)
    # Tampering happens AFTER signing so the signature no longer matches.
    if tamper:
        tamper(params)
    return "&".join(f"{k}={v}" for k, v in params.items())


def test_valid_init_data_returns_user() -> None:
    user = {"id": 777, "first_name": "T", "last_name": "X", "username": "tx"}
    result = verify_init_data(make_init_data(user=user), BOT_TOKEN, MAX_AGE)
    assert result["id"] == 777
    assert result["first_name"] == "T"


def test_valid_init_data_accepts_blank_query_id() -> None:
    data = make_init_data(extra={"query_id": ""})
    result = verify_init_data(data, BOT_TOKEN, MAX_AGE)
    assert result["id"] == 777


def test_wrong_token_is_rejected() -> None:
    data = make_init_data()
    with pytest.raises(InitDataError, match="signature mismatch"):
        verify_init_data(data, "999999:OTHER", MAX_AGE)


def test_tampered_user_is_rejected() -> None:
    data = make_init_data(tamper=lambda p: p.update(
        {"user": json.dumps({"id": 1, "first_name": "Attacker"})}
    ))
    with pytest.raises(InitDataError, match="signature mismatch"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_tampered_auth_date_is_rejected() -> None:
    data = make_init_data(tamper=lambda p: p.update({"auth_date": "100"}))
    with pytest.raises(InitDataError, match="signature mismatch"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_missing_hash_is_rejected() -> None:
    data = make_init_data(tamper=lambda p: p.pop("hash"))
    with pytest.raises(InitDataError, match="hash is missing"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_malformed_hash_is_rejected() -> None:
    data = make_init_data(tamper=lambda p: p.update({"hash": "nothex"}))
    with pytest.raises(InitDataError, match="not a valid sha256"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_duplicate_parameter_is_rejected() -> None:
    data = make_init_data()
    data = f"{data}&user=extra"  # duplicate user parameter
    with pytest.raises(InitDataError, match="duplicate"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_stale_auth_date_is_rejected() -> None:
    data = make_init_data(auth_age_seconds=MAX_AGE + 1)
    with pytest.raises(InitDataError, match="too old"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_fresh_auth_date_within_max_age_is_accepted() -> None:
    data = make_init_data(auth_age_seconds=MAX_AGE - 10)
    assert verify_init_data(data, BOT_TOKEN, MAX_AGE)["id"] == 777


def test_far_future_auth_date_is_rejected() -> None:
    # A correctly-signed auth_date far in the future (clock skew attack).
    data = make_init_data(auth_age_seconds=-3600)
    with pytest.raises(InitDataError, match="future"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_missing_auth_date_is_rejected() -> None:
    data = make_init_data(omit={"auth_date"})
    with pytest.raises(InitDataError, match="auth_date is missing"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_missing_user_is_rejected() -> None:
    data = make_init_data(omit={"user"})
    with pytest.raises(InitDataError, match="user is missing"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_invalid_user_json_is_rejected() -> None:
    data = make_init_data(raw_user="{not json")
    with pytest.raises(InitDataError, match="not valid JSON"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_non_integer_user_id_is_rejected() -> None:
    data = make_init_data(user={"id": "777", "first_name": "T"})
    with pytest.raises(InitDataError, match="user id is invalid"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_bot_user_is_rejected() -> None:
    data = make_init_data(user={"id": 777, "first_name": "Bot", "is_bot": True})
    with pytest.raises(InitDataError, match="bot users"):
        verify_init_data(data, BOT_TOKEN, MAX_AGE)


def test_empty_string_is_rejected() -> None:
    with pytest.raises(InitDataError, match="hash is missing"):
        verify_init_data("", BOT_TOKEN, MAX_AGE)
