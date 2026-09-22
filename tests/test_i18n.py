"""Per-user internationalization tests (SPEC i18n goal).

Covers the central translation service (registry, fallbacks, interpolation,
locale parity), the per-user language column (defaults, scoping, persistence,
no auto-override from Telegram), bot flows (start, /language, language
callback, immediate re-render), background jobs using the recipient's
language at execution time, the explicit AI language instruction, and the
Mini App API (settings read/update, validation, locale dictionaries).
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from assistant.bot import callbacks, keyboards
from assistant.bot.handlers import cmd_language, cmd_start, on_language
from assistant.i18n import (
    DEFAULT_LANGUAGE,
    FALLBACK_LANGUAGE,
    SUPPORTED_LANGUAGES,
    SupportedLanguage,
    for_language,
    is_supported,
    language_name,
    load_locale,
    t,
)
from assistant.i18n import service as i18n_service
from assistant.models.jobs import BackgroundJob, JobStatus
from assistant.models.users import User, UserSettings
from assistant.services import chat as chat_service
from assistant.services import digests
from assistant.services import jobs as jobs_service
from assistant.services import reminders as reminders_service
from assistant.services.users import upsert_user
from assistant.worker import registry
from test_api import HEADERS, client  # noqa: F401  (client fixture from test_api)


@pytest.mark.parametrize(
    "language,expected",
    [("ru", "Отменено."), ("en", "Cancelled."), ("fr", "Отменено.")],
)
def test_t_parametrized(language: str, expected: str) -> None:
    assert t(language, "common.cancelled") == expected

# ---------------------------------------------------------------------------
# Service: registry, lookup, fallbacks, interpolation
# ---------------------------------------------------------------------------


def test_registry_centralizes_languages() -> None:
    assert frozenset({"ru", "en"}) == SUPPORTED_LANGUAGES
    assert DEFAULT_LANGUAGE == "ru"
    assert FALLBACK_LANGUAGE == "ru"
    assert {m.value for m in SupportedLanguage} == {"ru", "en"}


def test_is_supported() -> None:
    assert is_supported("ru")
    assert is_supported("en")
    assert not is_supported("fr")
    assert not is_supported("")


def test_language_name_mapping() -> None:
    assert language_name("ru") == "Russian"
    assert language_name("en") == "English"
    assert language_name("zz") == "Russian"  # unknown -> fallback


def test_ru_and_en_lookup() -> None:
    assert t("ru", "common.cancelled") == "Отменено."
    assert t("en", "common.cancelled") == "Cancelled."


def test_unknown_language_falls_back_to_ru() -> None:
    assert t("fr", "common.cancelled") == t("ru", "common.cancelled")
    assert t("", "common.cancelled") == t("ru", "common.cancelled")


def test_missing_key_returns_key_without_crash() -> None:
    assert t("en", "no.such.key") == "no.such.key"
    assert t("ru", "no.such.key") == "no.such.key"


def test_key_missing_from_active_locale_falls_back_to_ru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ru = load_locale("ru")
    en = {k: v for k, v in load_locale("en").items() if k != "common.cancelled"}
    monkeypatch.setattr(
        i18n_service, "_load", lambda locale: ru if locale == "ru" else en
    )
    assert t("en", "common.cancelled") == ru["common.cancelled"]


def test_interpolation_and_safe_format() -> None:
    assert t("en", "start.welcome", name="Ann") == (
        "Hi Ann! I keep your tasks, events, workouts, and files.\n"
        "Pick a section below."
    )
    # Missing kwargs: the raw template is returned, never an exception.
    assert "{name}" in t("en", "start.welcome")
    assert t("ru", "reminders.notification", message="Call") == "Напоминание: Call"
    assert t("en", "reminders.notification", message="Call") == "Reminder: Call"


def test_locale_key_parity_ru_en() -> None:
    assert set(load_locale("ru")) == set(load_locale("en"))


def test_load_locale_returns_independent_copy() -> None:
    snapshot = load_locale("en")
    snapshot["common.cancelled"] = "mutated"
    assert load_locale("en")["common.cancelled"] == "Cancelled."


def test_translator_bound_to_language() -> None:
    tr = for_language("en")
    assert tr.get("common.cancelled") == "Cancelled."
    assert tr("common.cancelled") == "Cancelled."
    # Unknown code resolves to the fallback.
    assert for_language("zz").get("common.cancelled") == "Отменено."


# ---------------------------------------------------------------------------
# Per-user language column
# ---------------------------------------------------------------------------


async def _user(session: AsyncSession, user_id: int, **kwargs: object) -> User:
    user, _ = await upsert_user(session, user_id=user_id, **kwargs)
    await session.commit()
    return user


async def test_new_user_defaults_to_ru(session: AsyncSession) -> None:
    user = await _user(session, 91, first_name="N")
    assert user.settings is not None
    assert user.settings.language == "ru"


async def test_telegram_language_code_does_not_override(
    session: AsyncSession,
) -> None:
    # The bot path must never derive the persisted language from the
    # Telegram client's language_code: new users get the default.
    from assistant.bot.handlers import _ensure_user

    tg_en = SimpleNamespace(
        id=92, first_name="N", last_name=None, username=None,
        is_bot=False, language_code="en",
    )
    user = await _ensure_user(session, tg_en)
    await session.commit()
    assert user.settings.language == "ru"

    # And an existing user's chosen language survives upserts.
    user.settings.language = "en"
    await session.commit()
    tg_ru = SimpleNamespace(
        id=92, first_name="N", last_name=None, username=None,
        is_bot=False, language_code="ru",
    )
    again = await _ensure_user(session, tg_ru)
    await session.commit()
    assert again.settings.language == "en"


async def test_language_is_user_scoped(session: AsyncSession) -> None:
    alice = await _user(session, 93, first_name="A")
    bob = await _user(session, 94, first_name="B")
    alice.settings.language = "en"
    await session.commit()

    assert alice.settings.language == "en"
    assert bob.settings.language == "ru"
    assert t(alice.settings.language, "common.cancelled") == "Cancelled."
    assert t(bob.settings.language, "common.cancelled") == "Отменено."


async def test_language_persists_across_sessions(session: AsyncSession) -> None:
    user = await _user(session, 95, first_name="P")
    user.settings.language = "en"
    await session.commit()
    uid = user.id
    session.expire_all()

    # A fresh SELECT (as a new worker process would issue) reloads the row
    # from PostgreSQL, proving the language was committed, not just in memory.
    row = (
        await session.execute(
            select(UserSettings.language).where(UserSettings.user_id == uid)
        )
    ).scalar_one()
    assert row == "en"


# ---------------------------------------------------------------------------
# Bot flows
# ---------------------------------------------------------------------------


def _tg_user(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id, first_name="F", last_name=None, username=None, is_bot=False
    )


def _message(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(from_user=_tg_user(user_id), answer=AsyncMock())


def _state() -> SimpleNamespace:
    return SimpleNamespace(
        get_state=AsyncMock(return_value=None),
        get_data=AsyncMock(return_value={}),
        set_state=AsyncMock(),
        update_data=AsyncMock(),
        clear=AsyncMock(),
    )


def _callback(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=_tg_user(user_id),
        message=SimpleNamespace(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )


async def test_cmd_start_renders_in_user_language(session: AsyncSession) -> None:
    # The fake Telegram identity reports first_name "F".
    en_user = await _user(session, 96, first_name="F")
    en_user.settings.language = "en"
    await _user(session, 97, first_name="F")
    await session.commit()

    msg = _message(96)
    await cmd_start(msg, session, _state())
    assert msg.answer.await_args.args[0] == t("en", "start.welcome", name="F")

    msg = _message(97)
    await cmd_start(msg, session, _state())
    assert msg.answer.await_args.args[0] == t("ru", "start.welcome", name="F")


async def test_cmd_language_shows_keyboard(session: AsyncSession) -> None:
    await _user(session, 98, first_name="F")
    msg = _message(98)
    await cmd_language(msg, session, _state())

    args = msg.answer.await_args
    assert args.args[0] == t("ru", "settings.language_prompt")
    kb = args.kwargs["reply_markup"]
    buttons = [b for row in kb.inline_keyboard for b in row]
    codes = sorted(callbacks.LanguageCallback.unpack(b.callback_data).code for b in buttons)
    assert codes == ["en", "ru"]


async def test_on_language_switches_to_en_immediately(session: AsyncSession) -> None:
    user = await _user(session, 99, first_name="F")
    cb = _callback(99)
    await on_language(
        cb, callbacks.LanguageCallback(code="en"), session, _state()
    )
    await session.commit()

    assert user.settings.language == "en"
    args = cb.message.edit_text.await_args
    assert args.args[0] == t("en", "settings.language_changed", label="English")
    # Re-rendered in the new language.
    kb = args.kwargs["reply_markup"]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "🗣 Language" in labels
    cb.answer.assert_awaited_once()


async def test_on_language_rejects_unsupported_code(session: AsyncSession) -> None:
    user = await _user(session, 100, first_name="F")
    cb = _callback(100)
    await on_language(
        cb, callbacks.LanguageCallback(code="fr"), session, _state()
    )
    await session.commit()

    assert user.settings.language == "ru"
    args = cb.message.edit_text.await_args
    assert args.args[0] == t("ru", "errors.generic")


async def test_settings_keyboard_has_language_button() -> None:
    kb = keyboards.settings_kb("ru")
    buttons = [b for row in kb.inline_keyboard for b in row]
    # The language button is identified by its localized label.
    lang_btn = next(b for b in buttons if b.text == t("ru", "settings.language"))
    assert callbacks.SettingsCallback.unpack(lang_btn.callback_data).action == "language"


# ---------------------------------------------------------------------------
# Background jobs use the recipient's language AT EXECUTION TIME
# ---------------------------------------------------------------------------


async def _run_job_by_id(session: AsyncSession, job_id: int) -> None:
    job = await session.get(BackgroundJob, job_id)
    job.status = JobStatus.running.value
    await session.commit()
    handler = registry.handlers[job.type]
    await handler(session, job)
    await session.commit()


async def test_reminder_wrapper_language_at_execution(
    session: AsyncSession, monkeypatch
) -> None:
    user = await _user(session, 101, first_name="F")
    user.settings.digest_time = time(0, 0)
    sender = AsyncMock()
    monkeypatch.setattr("assistant.services.notifications.send_text", sender)

    # Scheduled while the user is Russian.
    r1 = await reminders_service.create_reminder(
        session, user,
        fire_at=datetime.now(UTC) - timedelta(minutes=1),
        message="Call",
    )
    await session.commit()
    await _run_job_by_id(session, r1.job_id)
    assert sender.await_args.args == (user.id, "Напоминание: Call")

    # The user switches to English; the next reminder is wrapped in English,
    # and the stored user text stays unchanged in both cases.
    user.settings.language = "en"
    r2 = await reminders_service.create_reminder(
        session, user,
        fire_at=datetime.now(UTC) + timedelta(minutes=1),
        message="Call",
    )
    await session.commit()
    assert r2.message == "Call"
    await _run_job_by_id(session, r2.job_id)
    assert sender.await_args.args == (user.id, "Reminder: Call")


async def test_digest_uses_language_at_execution(
    session: AsyncSession, monkeypatch
) -> None:
    user = await _user(session, 102, first_name="F")
    user.settings.digest_time = time(0, 0)
    sender = AsyncMock()
    monkeypatch.setattr("assistant.services.notifications.send_text", sender)

    # Scheduled while Russian, then the user switches to English.
    await digests.schedule_todays_digest(session, user)
    user.settings.language = "en"
    await session.commit()

    worker = "t-i18n"
    job = await jobs_service.claim_job(session, worker_id=worker)
    await session.commit()
    assert job is not None and job.type == digests.DIGEST_JOB_TYPE
    await registry.handlers[digests.DIGEST_JOB_TYPE](session, job)
    await session.commit()
    await jobs_service.complete_job(session, job.id, worker_id=worker)
    await session.commit()

    assert sender.await_args.args[0] == user.id
    assert "Good morning" in sender.await_args.args[1]


# ---------------------------------------------------------------------------
# AI: explicit language instruction, user text not translated, neutral schema
# ---------------------------------------------------------------------------


class _CapturingProvider:
    def __init__(self, reply: str = "ok") -> None:
        self.system: str | None = None
        self.messages: list[dict[str, str]] | None = None
        self.reply = reply

    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        self.system = system
        self.messages = messages
        return self.reply

    async def embed_query(self, *, query: str) -> list[float]:
        # No chunks are stored in these tests; the vector is unused.
        return [0.0] * 384


async def test_ai_system_prompt_states_explicit_language(
    session: AsyncSession,
) -> None:
    ru_user = await _user(session, 103, first_name="R")
    en_user = await _user(session, 104, first_name="E")
    en_user.settings.language = "en"
    await session.commit()

    prov = _CapturingProvider()
    await chat_service.chat(session, ru_user, "привет", provider=prov)
    assert "Always answer in Russian" in (prov.system or "")

    prov = _CapturingProvider()
    await chat_service.chat(session, en_user, "hi there", provider=prov)
    assert "Always answer in English" in (prov.system or "")
    # The user's own message is passed through untranslated.
    assert prov.messages[-1] == {"role": "user", "content": "hi there"}


async def test_draft_system_prompt_is_language_neutral() -> None:
    from assistant.ai.prompts import DRAFT_SYSTEM

    rendered = DRAFT_SYSTEM.format(context="ctx", tz="Europe/Berlin", now="2026-09-22")
    assert "Russian" not in rendered
    assert "English" not in rendered


# ---------------------------------------------------------------------------
# Mini App API: settings read/update + validation, locale dictionaries
# ---------------------------------------------------------------------------


async def test_api_settings_language_get_and_patch(
    client, session: AsyncSession
) -> None:
    res = await client.get("/api/v1/settings", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["language"] == "ru"

    res = await client.patch(
        "/api/v1/settings", headers=HEADERS, json={"language": "en"}
    )
    assert res.status_code == 200
    assert res.json()["language"] == "en"

    res = await client.get("/api/v1/settings", headers=HEADERS)
    assert res.json()["language"] == "en"


async def test_api_settings_invalid_language_is_422(
    client: object,
) -> None:
    res = await client.patch(
        "/api/v1/settings", headers=HEADERS, json={"language": "fr"}
    )
    assert res.status_code == 422
    res = await client.get("/api/v1/settings", headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["language"] == "ru"


async def test_api_i18n_endpoints(client) -> None:
    res = await client.get("/api/v1/i18n/languages", headers=HEADERS)
    assert res.status_code == 200
    data = res.json()
    assert {row["code"] for row in data} == {"ru", "en"}
    assert all(row["label"] for row in data)

    res = await client.get("/api/v1/i18n/en", headers=HEADERS)
    assert res.status_code == 200
    body = res.json()
    assert body["common.cancelled"] == "Cancelled."
    assert body == load_locale("en")

    res = await client.get("/api/v1/i18n/fr", headers=HEADERS)
    assert res.status_code == 404
