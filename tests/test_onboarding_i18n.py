"""Onboarding i18n and FSM state-isolation tests (SPEC §4, §5, §31).

A brand-new user gets Russian (the default language) everywhere in the
onboarding FSM: settings prompts (timezone, digest time), cancellation, and
task-creation prompts/validation. English users get the English equivalents.
Validation errors are localized — no hardcoded English UI strings leak to a
Russian user.

Also covers FSM state isolation: a task message sent while in a settings
state is parsed as the settings value (never routed to the AI), and a
settings-looking value sent while in the task-draft state is treated as a
task, and /cancel aborts any in-progress state.
"""

from __future__ import annotations

from datetime import datetime, time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from assistant.ai import AITaskDraft
from assistant.bot import handlers
from assistant.bot.callbacks import MenuCallback, SettingsCallback
from assistant.bot.handlers import cmd_cancel, cmd_start, on_menu, on_settings, on_text
from assistant.bot.states import (
    SettingsStates,
    TaskDraftStates,
    WorkoutStates,
)
from assistant.services.users import upsert_user

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


class _SpyProvider:
    """Records AI calls and returns a fixed draft (must never be hit by
    settings-state traffic)."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *, system: str, messages: list[dict[str, str]]) -> str:
        return ""

    async def chat_structured(
        self, *, system: str, messages: list[dict[str, str]], schema: type[Any]
    ) -> AITaskDraft:
        self.calls += 1
        assert schema is AITaskDraft
        return AITaskDraft(
            title="Позвонить Сергею",
            start=datetime(2026, 9, 22, 17, 0),
            reminder_offsets=[0],
        )


def _fake_tg_user(
    user_id: int = 41, first_name: str = "Надя"
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        first_name=first_name,
        last_name=None,
        username="ndya",
        is_bot=False,
    )


def _fake_message(
    text: str, user_id: int = 41, first_name: str = "Надя"
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        from_user=_fake_tg_user(user_id, first_name),
        chat=SimpleNamespace(id=100),
        bot=SimpleNamespace(id=777, send_chat_action=AsyncMock()),
        answer=AsyncMock(),
    )


def _fake_state(state_value: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_state=AsyncMock(return_value=state_value),
        get_data=AsyncMock(return_value={}),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )


def _fake_callback(user_id: int = 41) -> SimpleNamespace:
    return SimpleNamespace(
        from_user=_fake_tg_user(user_id),
        message=SimpleNamespace(edit_text=AsyncMock(), text=""),
        answer=AsyncMock(),
    )


def _last_text(message: SimpleNamespace) -> str:
    return message.answer.await_args.args[0]


# ---------------------------------------------------------------------------
# Russian (default) onboarding
# ---------------------------------------------------------------------------


async def test_ru_new_user_welcome_is_russian(session) -> None:
    message = _fake_message("/start")
    state = _fake_state()
    await cmd_start(message, session, state)
    await session.commit()
    # V5.4 P6: /start sends welcome + menu in two messages; the welcome is
    # the first one.
    welcome = message.answer.await_args_list[0].args[0]
    assert "Привет" in welcome
    assert "Hi " not in welcome


async def test_ru_timezone_prompt_and_set_are_russian(session) -> None:
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()
    cb = _fake_callback()
    state = _fake_state()
    await on_settings(cb, SettingsCallback(action="timezone"), session, state)
    assert state.set_state.await_args.args[0] == SettingsStates.timezone.state
    prompt = cb.message.edit_text.await_args.args[0]
    assert prompt == (
        "Отправь IANA-зону, например Europe/Berlin. /cancel — отмена."
    )

    # Now answer the prompt with a valid zone: confirmation is in Russian.
    message = _fake_message("Europe/Berlin")
    await on_text(message, session, _fake_state(SettingsStates.timezone.state))
    await session.commit()
    reply = _last_text(message)
    assert reply == "Часовой пояс: Europe/Berlin."
    assert "Timezone set to" not in reply


async def test_ru_invalid_timezone_is_russian(session) -> None:
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()
    message = _fake_message("Mars/Olympus")
    await on_text(message, session, _fake_state(SettingsStates.timezone.state))
    assert "некорректная IANA-зона" in _last_text(message)
    assert "not a valid" not in _last_text(message)


async def test_ru_digest_time_prompt_set_and_invalid_are_russian(session) -> None:
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()
    cb = _fake_callback()
    state = _fake_state()
    await on_settings(
        cb, SettingsCallback(action="digest_time"), session, state
    )
    prompt = cb.message.edit_text.await_args.args[0]
    assert prompt == (
        "Отправь время дайджеста в формате ЧЧ:ММ (24 часа). /cancel — отмена."
    )

    message = _fake_message("08:30")
    await on_text(message, session, _fake_state(SettingsStates.digest_time.state))
    await session.commit()
    assert _last_text(message) == "Время дайджеста: 08:30."

    message = _fake_message("25:99")
    await on_text(message, session, _fake_state(SettingsStates.digest_time.state))
    assert "Используй 24-часовой формат" in _last_text(message)
    assert "Use the 24-hour" not in _last_text(message)


async def test_ru_cancel_is_russian_and_clears_state(session) -> None:
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()
    state = _fake_state(TaskDraftStates.waiting_for_text.state)
    message = _fake_message("/cancel")
    await cmd_cancel(message, session, state)
    assert _last_text(message) == "Отменено."
    state.clear.assert_awaited_once()


async def test_ru_task_creation_prompt_and_preview_are_russian(session) -> None:
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()
    cb = _fake_callback()
    state = _fake_state()
    await on_menu(cb, MenuCallback(section="task"), session, state)
    assert state.set_state.await_args.args[0] == TaskDraftStates.waiting_for_text.state
    help_text = cb.message.edit_text.await_args.args[0]
    assert "Опиши задачу обычным языком" in help_text

    draft_text = (
        "title: Купить продукты\n"
        "date: 2026-09-22\n"
        "time: 18:30\n"
        "remind: 0\n"
    )
    message = _fake_message(draft_text)
    await on_text(message, session, _fake_state(TaskDraftStates.waiting_for_text.state))
    await session.commit()
    preview = _last_text(message)
    assert "Купить продукты" in preview
    assert "начало: 2026-09-22 18:30" in preview
    assert "start:" not in preview
    # The confirm keyboard labels are localized too.
    markup = message.answer.await_args.kwargs["reply_markup"]
    labels = [btn.text for row in markup.inline_keyboard for btn in row]
    assert "✅ Подтвердить" in labels


async def test_ru_workout_validation_error_is_russian(session) -> None:
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()
    message = _fake_message("Бег, abc")
    await on_text(message, session, _fake_state(WorkoutStates.waiting_log.state))
    reply = _last_text(message)
    assert "Минуты — целое число." in reply
    assert "Minutes must" not in reply
    # The prompt repeats in Russian as guidance.
    assert "Отправь: название, минуты, усилие" in reply


async def test_ru_fact_validation_error_is_russian(session) -> None:
    from assistant.bot.handlers import cmd_remember

    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()
    message = _fake_message("/remember " + "x" * 2001)
    await cmd_remember(message, session)
    reply = _last_text(message)
    assert "Слишком длинно (максимум 2000 символов)." in reply
    assert "too long" not in reply.lower()


# ---------------------------------------------------------------------------
# English onboarding
# ---------------------------------------------------------------------------


async def test_en_onboarding_strings_are_english(session) -> None:
    user, _ = await upsert_user(session, user_id=41, first_name="Nadya")
    user.settings.language = "en"
    await session.commit()

    # Welcome (V5.4 P6: the first of /start's two messages).
    message = _fake_message("/start", first_name="Nadya")
    await cmd_start(message, session, _fake_state())
    welcome = message.answer.await_args_list[0].args[0]
    assert "Hi Nadya!" in welcome
    assert "Привет" not in welcome

    # Timezone prompt + confirmation.
    cb = _fake_callback()
    state = _fake_state()
    await on_settings(cb, SettingsCallback(action="timezone"), session, state)
    prompt = cb.message.edit_text.await_args.args[0]
    assert prompt == "Send the IANA timezone, e.g. Europe/Berlin. /cancel aborts."

    message = _fake_message("Europe/Berlin")
    await on_text(message, session, _fake_state(SettingsStates.timezone.state))
    assert _last_text(message) == "Timezone set to Europe/Berlin."

    # Digest prompt + confirmation + invalid.
    cb = _fake_callback()
    await on_settings(cb, SettingsCallback(action="digest_time"), session, _fake_state())
    prompt = cb.message.edit_text.await_args.args[0]
    assert prompt == "Send the digest time as HH:MM (24-hour). /cancel aborts."

    message = _fake_message("08:30")
    await on_text(message, session, _fake_state(SettingsStates.digest_time.state))
    assert _last_text(message) == "Digest time set to 08:30."

    message = _fake_message("nope")
    await on_text(message, session, _fake_state(SettingsStates.digest_time.state))
    assert "Use the 24-hour HH:MM format" in _last_text(message)

    # Cancel.
    state = _fake_state(SettingsStates.timezone.state)
    message = _fake_message("/cancel")
    await cmd_cancel(message, session, state)
    assert _last_text(message) == "Cancelled."
    state.clear.assert_awaited_once()


async def test_en_task_creation_preview_is_english(session) -> None:
    user, _ = await upsert_user(session, user_id=41, first_name="Nadya")
    user.settings.language = "en"
    await session.commit()

    message = _fake_message(
        "title: Buy groceries\ndate: 2026-09-22\ntime: 18:30\nremind: 0\n"
    )
    await on_text(message, session, _fake_state(TaskDraftStates.waiting_for_text.state))
    await session.commit()
    preview = _last_text(message)
    assert "Buy groceries" in preview
    assert "start: 2026-09-22 18:30" in preview
    assert "начало:" not in preview


async def test_en_workout_validation_error_is_english(session) -> None:
    user, _ = await upsert_user(session, user_id=41, first_name="Nadya")
    user.settings.language = "en"
    await session.commit()
    message = _fake_message("Running, abc")
    await on_text(message, session, _fake_state(WorkoutStates.waiting_log.state))
    reply = _last_text(message)
    assert "Minutes must be a whole number." in reply
    assert "Минуты" not in reply


# ---------------------------------------------------------------------------
# FSM state isolation
# ---------------------------------------------------------------------------


async def test_task_text_in_digest_state_is_settings_value_not_ai(
    session, monkeypatch
) -> None:
    """A task-like message while in the digest-time state is parsed (and
    rejected) as a digest time; the AI provider must never be called."""
    spy = _SpyProvider()
    monkeypatch.setattr(handlers.chat, "get_ai_provider", lambda: spy)
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()

    message = _fake_message("Мне нужно сегодня позвонить Сергею в 17:00")
    await on_text(message, session, _fake_state(SettingsStates.digest_time.state))
    assert "Используй 24-часовой формат" in _last_text(message)
    assert spy.calls == 0


async def test_task_text_in_timezone_state_is_settings_value_not_ai(
    session, monkeypatch
) -> None:
    spy = _SpyProvider()
    monkeypatch.setattr(handlers.chat, "get_ai_provider", lambda: spy)
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()

    message = _fake_message("Сегодня напомни мне позвонить Сергею в 17:00")
    await on_text(message, session, _fake_state(SettingsStates.timezone.state))
    assert "некорректная IANA-зона" in _last_text(message)
    assert spy.calls == 0


async def test_settings_value_in_task_state_is_treated_as_task(
    session, monkeypatch
) -> None:
    """A settings-looking value (HH:MM) sent while in the task-draft state
    goes to the task flow, not to settings."""
    spy = _SpyProvider()
    monkeypatch.setattr(handlers.chat, "get_ai_provider", lambda: spy)
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()

    message = _fake_message("17:00")
    state = _fake_state(TaskDraftStates.waiting_for_text.state)
    await on_text(message, session, state)
    await session.commit()
    assert spy.calls == 1
    state.set_state.assert_awaited_once_with(TaskDraftStates.confirm.state)
    assert "Позвонить Сергею" in _last_text(message)
    # The digest/timezone settings must not have been touched.
    user, _ = await upsert_user(session, user_id=41, first_name="Надя")
    assert user.settings.digest_time == time(8, 0)


async def test_cancel_in_task_state_clears_state_without_ai(
    session, monkeypatch
) -> None:
    spy = _SpyProvider()
    monkeypatch.setattr(handlers.chat, "get_ai_provider", lambda: spy)
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()

    state = _fake_state(TaskDraftStates.waiting_for_text.state)
    message = _fake_message("/cancel")
    await cmd_cancel(message, session, state)
    assert _last_text(message) == "Отменено."
    state.clear.assert_awaited_once()
    assert spy.calls == 0


async def test_cancel_in_workout_schedule_state_clears_state(session) -> None:
    await upsert_user(session, user_id=41, first_name="Надя")
    await session.commit()
    state = _fake_state(WorkoutStates.waiting_schedule.state)
    message = _fake_message("/cancel")
    await cmd_cancel(message, session, state)
    assert _last_text(message) == "Отменено."
    state.clear.assert_awaited_once()
