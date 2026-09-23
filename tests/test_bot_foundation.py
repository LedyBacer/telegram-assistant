"""Credential-free tests for the bot foundation (SPEC §5-§7)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from assistant.bot import callbacks, handlers, keyboards
from assistant.bot.handlers import TaskDraft, _parse_draft
from assistant.bot.states import TaskDraftStates
from assistant.models.calendar_items import ItemKind, ItemPriority
from assistant.services.users import upsert_user

TZ = ZoneInfo("Europe/Berlin")


def _parse(text: str) -> TaskDraft:
    return _parse_draft(text, TZ)


class TestCallbacks:
    def test_menu_roundtrip(self) -> None:
        packed = callbacks.MenuCallback(section="today").pack()
        assert callbacks.MenuCallback.unpack(packed) == callbacks.MenuCallback(
            section="today"
        )

    def test_settings_roundtrip(self) -> None:
        packed = callbacks.SettingsCallback(action="digest_time").pack()
        assert callbacks.SettingsCallback.unpack(packed) == callbacks.SettingsCallback(
            action="digest_time"
        )

    def test_item_roundtrip(self) -> None:
        packed = callbacks.ItemCallback(action="complete", item_id=42).pack()
        assert callbacks.ItemCallback.unpack(packed) == callbacks.ItemCallback(
            action="complete", item_id=42
        )

    def test_draft_roundtrip(self) -> None:
        packed = callbacks.DraftCallback(action="confirm").pack()
        assert callbacks.DraftCallback.unpack(packed) == callbacks.DraftCallback(
            action="confirm"
        )


class TestKeyboards:
    def test_main_menu_without_base_url(self) -> None:
        kb = keyboards.main_menu_kb("ru")
        flat = [b for row in kb.inline_keyboard for b in row]
        assert len(flat) == 7  # miniapp button hidden without base_url
        assert all(b.callback_data is not None for b in flat)
        data = {callbacks.MenuCallback.unpack(b.callback_data).section for b in flat}
        assert data == {s for s, _ in keyboards.SECTIONS if s != "miniapp"}

    def test_main_menu_with_base_url(self) -> None:
        kb = keyboards.main_menu_kb("ru", "https://example.test/miniapp")
        flat = [b for row in kb.inline_keyboard for b in row]
        assert len(flat) == 8
        web = [b for b in flat if b.web_app is not None]
        assert len(web) == 1
        assert web[0].web_app.url == "https://example.test/miniapp"

    def test_settings_and_draft_keyboards(self) -> None:
        s = [b for row in keyboards.settings_kb("ru").inline_keyboard for b in row]
        assert [callbacks.SettingsCallback.unpack(b.callback_data) for b in s[:3]] == [
            callbacks.SettingsCallback(action="timezone"),
            callbacks.SettingsCallback(action="digest_time"),
            callbacks.SettingsCallback(action="language"),
        ]
        d = [b for row in keyboards.draft_kb("ru").inline_keyboard for b in row]
        assert [callbacks.DraftCallback.unpack(b.callback_data) for b in d] == [
            callbacks.DraftCallback(action="confirm"),
            callbacks.DraftCallback(action="cancel"),
        ]


class TestParseDraft:
    def test_title_only(self) -> None:
        d = _parse("title: Buy milk")
        assert d.title == "Buy milk"
        assert d.kind is ItemKind.task
        assert d.priority is ItemPriority.normal
        assert d.starts_at is None
        assert d.due_at is None
        assert d.description is None

    def test_full_fields(self) -> None:
        d = _parse(
            "title: Dentist\n"
            "date: 2026-09-21\n"
            "time: 18:30\n"
            "due: 2026-09-21 19:00\n"
            "priority: high\n"
            "kind: event\n"
            "description: bring passport\n"
        )
        assert d.kind is ItemKind.event
        assert d.priority is ItemPriority.high
        assert d.starts_at == datetime(2026, 9, 21, 18, 30, tzinfo=TZ).astimezone(
            UTC
        )
        assert d.due_at == datetime(2026, 9, 21, 19, 0, tzinfo=TZ).astimezone(UTC)
        assert d.description == "bring passport"

    def test_defaults_for_partial_date_time(self) -> None:
        assert _parse("title: Gym\ndate: 2026-09-21").starts_at == datetime(
            2026, 9, 21, 9, 0, tzinfo=TZ
        ).astimezone(UTC)
        parsed = _parse("title: Gym\ntime: 23:59")
        assert parsed.starts_at is not None
        assert parsed.starts_at.hour in (21, 22, 23)  # depends on UTC offset

    def test_comments_and_blank_lines_ignored(self) -> None:
        d = _parse("# note\n\ntitle: ok\n")
        assert d.title == "ok"

    @pytest.mark.parametrize(
        "text",
        [
            "no key separator",
            "title:",
            "kind: meeting",
            "priority: urgent",
            "date: not-a-date",
            "time: 25:00",
            "due: 2026-13-01 10:00",
        ],
    )
    def test_invalid(self, text: str) -> None:
        with pytest.raises(ValueError):
            _parse(text)

    def test_missing_title(self) -> None:
        with pytest.raises(ValueError):
            _parse("description: no title here")

    def test_title_too_long(self) -> None:
        with pytest.raises(ValueError):
            _parse(f"title: {'x' * 501}")

    def test_description_too_long(self) -> None:
        with pytest.raises(ValueError):
            _parse(f"title: ok\ndescription: {'x' * 4001}")


class TestPlainTextOutput:
    """P23: Telegram output is safe by default — plain text, never HTML."""

    _FAKE_TOKEN = "42:TEST-TOKEN"

    def test_bot_process_bot_has_no_parse_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from assistant.bot.main import create_bot
        from assistant.config import get_settings

        monkeypatch.setattr(get_settings(), "telegram_bot_token", self._FAKE_TOKEN)
        bot = create_bot()
        assert bot.default is not None
        assert bot.default.parse_mode is None

    def test_notification_bot_has_no_parse_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from assistant.config import get_settings
        from assistant.services import notifications

        monkeypatch.setattr(get_settings(), "telegram_bot_token", self._FAKE_TOKEN)
        monkeypatch.setattr(notifications, "_bot", None)
        try:
            bot = notifications._get_bot()
        finally:
            monkeypatch.setattr(notifications, "_bot", None)
        assert bot.default is not None
        assert bot.default.parse_mode is None

    @pytest.mark.parametrize(
        "payload",
        [
            "plain",
            "<b>bold?</b>",
            "<script>alert('x')</script>",
            "Tom & Jerry's <note> \"quoted\"",
            "5 < 6 and 6 > 5",
            "https://example.com/a?b=1&c=2",
        ],
    )
    async def test_send_text_sends_verbatim_without_markup(
        self, monkeypatch: pytest.MonkeyPatch, payload: str
    ) -> None:
        """send_text must forward the exact text with no parse_mode override:
        with plain-text delivery, markup-looking user content is inert."""
        from assistant.config import get_settings
        from assistant.services import notifications

        monkeypatch.setattr(get_settings(), "telegram_bot_token", self._FAKE_TOKEN)
        sent: list[tuple[str, dict]] = []

        async def _capture(chat_id: int, text: str, **kwargs: object) -> None:
            sent.append((text, kwargs))

        bot = notifications._get_bot()
        monkeypatch.setattr(notifications, "_bot", bot)
        monkeypatch.setattr(bot, "send_message", _capture)
        try:
            await notifications.send_text(1, payload)
        finally:
            monkeypatch.setattr(notifications, "_bot", None)

        assert sent == [(payload, {})]


def _fake_tg_user(user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id, first_name="Al", last_name="Tester", username="al", is_bot=False
    )


def _fake_message(text: str, user_id: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        text=text, from_user=_fake_tg_user(user_id), answer=AsyncMock()
    )


def _fake_state(state_value: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        get_state=AsyncMock(return_value=state_value),
        get_data=AsyncMock(return_value={}),
        update_data=AsyncMock(),
        set_state=AsyncMock(),
        clear=AsyncMock(),
    )


async def _truncate_users(session) -> None:
    # Run in the session's own transaction: a TRUNCATE from a second
    # connection would deadlock on this session's open transaction locks.
    await session.execute(text("TRUNCATE users RESTART IDENTITY CASCADE"))
    await session.commit()


async def test_upsert_user_creates_then_updates(session) -> None:
    await _truncate_users(session)
    try:
        user, created = await upsert_user(
            session, user_id=7, first_name="First", username="first"
        )
        assert created is True
        assert user.id == 7
        assert user.settings is not None
        await session.commit()

        user2, created2 = await upsert_user(
            session, user_id=7, first_name="Renamed", username="renamed"
        )
        assert created2 is False
        assert user2.first_name == "Renamed"
        count = (
            await session.execute(text("SELECT count(*) FROM users WHERE id = 7"))
        ).scalar_one()
        assert count == 1
        await session.commit()
    finally:
        await _truncate_users(session)


async def test_on_text_stores_chat_message(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assistant.bot.handlers import on_text

    # This test asserts a single outgoing message; the thinking status UX
    # is covered in tests/test_thinking_ux.py.
    monkeypatch.setattr(
        handlers,
        "get_settings",
        lambda: SimpleNamespace(
            chat_thinking_enabled=False, public_base_url="https://app.test"
        ),
    )
    await _truncate_users(session)
    try:
        message = _fake_message("remember: finish the report")
        state = _fake_state()
        await on_text(message, session, state)
        await session.commit()

        row = (
            await session.execute(
                text("SELECT role, content, source FROM chat_messages")
            )
        ).one()
        assert row == ("user", "remember: finish the report", "telegram")
        message.answer.assert_awaited_once()
        state.set_state.assert_not_awaited()
    finally:
        await _truncate_users(session)


async def test_on_text_task_draft_flow_confirm_persists_item(session) -> None:
    from assistant.bot.handlers import on_draft, on_text

    await _truncate_users(session)
    try:
        # Step 1: user sends the structured task while in waiting_for_text.
        state = _fake_state(TaskDraftStates.waiting_for_text.state)
        await on_text(_fake_message("title: Dentist\npriority: high"), session, state)
        state.update_data.assert_awaited_once()
        await session.commit()

        # Step 2: confirm callback persists the item.
        callback = SimpleNamespace(
            from_user=_fake_tg_user(),
            message=SimpleNamespace(edit_text=AsyncMock()),
            answer=AsyncMock(),
        )
        data_state = SimpleNamespace(
            get_state=AsyncMock(return_value=None),
            get_data=AsyncMock(return_value={"draft_text": "title: Dentist\npriority: high"}),
            update_data=AsyncMock(),
            set_state=AsyncMock(),
            clear=AsyncMock(),
        )
        await on_draft(
            callback, callbacks.DraftCallback(action="confirm"), session, data_state
        )
        await session.commit()

        item = (
            await session.execute(
                text(
                    "SELECT title, kind, priority, status, source FROM calendar_items"
                )
            )
        ).one()
        assert item == ("Dentist", "task", "high", "scheduled", "bot")
        callback.answer.assert_awaited_once()
    finally:
        await session.execute(text("TRUNCATE calendar_items RESTART IDENTITY CASCADE"))
        await _truncate_users(session)


class TestPrivateChatsOnly:
    """V3 P25: the bot is restricted to private chats; groups/channels get a
    localized rejection and no user/item/state is ever created from them."""

    def _fake_message(self, chat_type: str, user_id: int = 1) -> SimpleNamespace:
        return SimpleNamespace(
            text="hello",
            chat=SimpleNamespace(id=100, type=chat_type),
            from_user=_fake_tg_user(user_id),
            answer=AsyncMock(),
        )

    def _fake_callback(self, chat_type: str, user_id: int = 1) -> SimpleNamespace:
        return SimpleNamespace(
            from_user=_fake_tg_user(user_id),
            message=SimpleNamespace(chat=SimpleNamespace(id=100, type=chat_type)),
            answer=AsyncMock(),
        )

    async def test_group_message_rejected_localized_no_user_created(
        self, session
    ) -> None:
        from assistant.i18n import DEFAULT_LANGUAGE, t

        await _truncate_users(session)
        try:
            message = self._fake_message("group")
            await handlers.reject_non_private_chat(message, session)
            await session.commit()

            message.answer.assert_awaited_once_with(
                t(DEFAULT_LANGUAGE, "chat.private_only")
            )
            count = (
                await session.execute(text("SELECT count(*) FROM users"))
            ).scalar_one()
            assert count == 0
        finally:
            await _truncate_users(session)

    async def test_group_rejection_uses_existing_user_language(self, session) -> None:
        from assistant.i18n import t

        await _truncate_users(session)
        try:
            user, _ = await upsert_user(session, user_id=9, first_name="Bea")
            user.settings.language = "en"
            await session.commit()

            message = self._fake_message("supergroup", user_id=9)
            await handlers.reject_non_private_chat(message, session)
            message.answer.assert_awaited_once_with(t("en", "chat.private_only"))
        finally:
            await _truncate_users(session)

    async def test_group_callback_rejected_with_alert(self, session) -> None:
        from assistant.i18n import DEFAULT_LANGUAGE, t

        await _truncate_users(session)
        try:
            callback = self._fake_callback("channel")
            await handlers.reject_non_private_callback(callback, session)
            await session.commit()

            callback.answer.assert_awaited_once_with(
                t(DEFAULT_LANGUAGE, "chat.private_only"), show_alert=True
            )
            count = (
                await session.execute(text("SELECT count(*) FROM users"))
            ).scalar_one()
            assert count == 0
        finally:
            await _truncate_users(session)

    async def test_main_router_filters_allow_only_private_chats(self) -> None:
        from magic_filter import AttrDict

        def chat_ctx(kind: str) -> AttrDict:
            return AttrDict({"chat": SimpleNamespace(type=kind)})

        message_filter = handlers.router.message.filter
        assert message_filter is not None
        assert bool(message_filter.resolve(chat_ctx("private")))
        for kind in ("group", "supergroup", "channel"):
            assert not bool(message_filter.resolve(chat_ctx(kind)))

        callback_filter = handlers.router.callback_query.filter
        assert callback_filter is not None

        def callback_ctx(kind: str) -> AttrDict:
            return AttrDict(
                {"message": SimpleNamespace(chat=SimpleNamespace(type=kind))}
            )

        assert bool(callback_filter.resolve(callback_ctx("private")))
        for kind in ("group", "supergroup", "channel"):
            assert not bool(callback_filter.resolve(callback_ctx(kind)))

    def test_guard_router_wired_before_main_router(self) -> None:
        import inspect

        from assistant.bot import main

        src = inspect.getsource(main._run)
        assert src.index("include_router(private_guard)") < src.index(
            "include_router(router)"
        )


def test_time_zone_conversion_utc() -> None:
    # Sanity check used by _parse_draft expectations.
    assert datetime(2026, 9, 21, 18, 30, tzinfo=TZ).astimezone(UTC) == datetime(
        2026, 9, 21, 16, 30, tzinfo=UTC
    )
    assert time(23, 59) is not None
