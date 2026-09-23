"""Unit tests for the central Telegram text-delivery helpers (V3 P24)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from assistant.services.tg_text import (
    TELEGRAM_TEXT_LIMIT,
    answer_long,
    send_long,
    split_for_telegram,
)

FAKE_TOKEN = "42:TEST-TOKEN"


class TestSplitForTelegram:
    def test_short_text_is_single_segment(self) -> None:
        assert split_for_telegram("hello") == ["hello"]

    def test_empty_text_yields_no_segments(self) -> None:
        assert split_for_telegram("") == []

    def test_exactly_at_limit_is_one_segment(self) -> None:
        text = "x" * TELEGRAM_TEXT_LIMIT
        assert split_for_telegram(text) == [text]

    def test_over_limit_is_split_within_bound(self) -> None:
        text = "x" * (TELEGRAM_TEXT_LIMIT * 2 + 10)
        segments = split_for_telegram(text)
        assert len(segments) > 1
        assert all(len(seg) <= TELEGRAM_TEXT_LIMIT for seg in segments)
        assert "".join(segments) == text  # no content lost

    def test_prefers_paragraph_boundary(self) -> None:
        # Two paragraphs; the paragraph break sits well inside the limit.
        text = ("para one\nline a\n" + "y" * 100) + "\n\n" + ("para two\n" + "z" * 200)
        limit = 300
        segments = split_for_telegram(text, limit=limit)
        assert all(len(s) <= limit for s in segments)
        # The split happens at the \n\n boundary: first segment ends with it.
        assert segments[0].endswith("\n\n")
        assert "".join(segments) == text

    def test_prefers_newline_over_hard_cut(self) -> None:
        # No double newline; a single \n near the end of the window wins.
        text = "a\n" * (TELEGRAM_TEXT_LIMIT - 1) + "tail"
        segments = split_for_telegram(text)
        assert all(len(s) <= TELEGRAM_TEXT_LIMIT for s in segments)
        assert segments[0].endswith("\n")
        assert "".join(segments) == text

    def test_hard_cut_when_no_boundary_fits(self) -> None:
        text = "n" * (TELEGRAM_TEXT_LIMIT + 5)
        segments = split_for_telegram(text)
        assert all(len(s) <= TELEGRAM_TEXT_LIMIT for s in segments)
        assert "".join(segments) == text

    def test_many_paragraphs_all_within_limit(self) -> None:
        text = "\n\n".join(f"paragraph {i}\n" + "w" * 90 for i in range(60))
        segments = split_for_telegram(text)
        assert all(len(s) <= TELEGRAM_TEXT_LIMIT for s in segments)
        assert "".join(segments) == text

    def test_limit_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            split_for_telegram("abc", limit=0)


class TestSendLong:
    async def test_short_text_is_single_message(self) -> None:
        sent: list[tuple[int, str]] = []

        async def _capture(chat_id: int, text: str, **kwargs: object) -> None:
            sent.append((chat_id, text))

        await send_long(SimpleNamespace(send_message=_capture), 7, "hi")
        assert sent == [(7, "hi")]

    async def test_long_text_is_multiple_messages_within_limit(self) -> None:
        sent: list[str] = []

        async def _capture(chat_id: int, text: str, **kwargs: object) -> None:
            sent.append(text)

        text = "\n\n".join(f"block {i}\n" + "d" * 200 for i in range(40))
        assert len(text) > TELEGRAM_TEXT_LIMIT
        await send_long(SimpleNamespace(send_message=_capture), 7, text)
        assert len(sent) > 1
        assert all(len(s) <= TELEGRAM_TEXT_LIMIT for s in sent)
        assert "".join(sent) == text

    async def test_empty_text_sends_nothing(self) -> None:
        sent: list[str] = []

        async def _capture(chat_id: int, text: str, **kwargs: object) -> None:
            sent.append(text)

        await send_long(SimpleNamespace(send_message=_capture), 7, "")
        assert sent == []

    async def test_failure_propagates_for_requeue(self) -> None:
        async def _boom(chat_id: int, text: str, **kwargs: object) -> None:
            raise RuntimeError("network down")

        with pytest.raises(RuntimeError):
            await send_long(SimpleNamespace(send_message=_boom), 7, "hi")


class TestAnswerLong:
    async def test_keyboard_attaches_to_last_segment_only(self) -> None:
        sent: list[tuple[str, object]] = []

        async def _answer(text: str, **kw: object) -> None:
            sent.append((text, kw.get("reply_markup")))

        message = SimpleNamespace(answer=_answer)
        text = "\n\n".join(f"para {i}\n" + "q" * 200 for i in range(40))
        assert len(text) > TELEGRAM_TEXT_LIMIT
        markup = object()
        await answer_long(message, text, reply_markup=markup)
        assert len(sent) > 1
        for _seg, mk in sent[:-1]:
            assert mk is None
        assert sent[-1][1] is markup
        assert "".join(seg for seg, _ in sent) == text

    async def test_short_text_single_message_with_keyboard(self) -> None:
        sent: list[tuple[str, object]] = []

        async def _answer(text: str, **kw: object) -> None:
            sent.append((text, kw.get("reply_markup")))

        message = SimpleNamespace(answer=_answer)
        markup = object()
        await answer_long(message, "short reply", reply_markup=markup)
        assert sent == [("short reply", markup)]

    async def test_no_keyboard_means_no_markup_kwarg(self) -> None:
        kwargs_seen: list[dict] = []

        async def _answer(text: str, **kw: object) -> None:
            kwargs_seen.append(kw)

        message = SimpleNamespace(answer=_answer)
        await answer_long(message, "no keyboard")
        assert kwargs_seen == [{}]


class TestSendTextIntegration:
    """send_text (worker path) must split long digests/reminders."""

    async def test_long_text_splits_across_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from assistant.config import get_settings
        from assistant.services import notifications

        monkeypatch.setattr(get_settings(), "telegram_bot_token", FAKE_TOKEN)
        sent: list[str] = []

        async def _capture(chat_id: int, text: str, **kwargs: object) -> None:
            sent.append(text)

        bot = notifications._get_bot()
        monkeypatch.setattr(notifications, "_bot", bot)
        monkeypatch.setattr(bot, "send_message", _capture)
        try:
            text = "\n\n".join(f"line {i}\n" + "e" * 120 for i in range(80))
            assert len(text) > TELEGRAM_TEXT_LIMIT
            await notifications.send_text(42, text)
        finally:
            monkeypatch.setattr(notifications, "_bot", None)

        assert len(sent) > 1
        assert all(len(s) <= TELEGRAM_TEXT_LIMIT for s in sent)
        assert "".join(sent) == text
