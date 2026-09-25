"""Telegram rendering for model-authored Markdown replies."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from assistant.services.tg_markdown import (
    answer_long_markdown,
    markdown_to_telegram_html,
)


class TestMarkdownToTelegramHtml:
    def test_qwen_style_markdown(self) -> None:
        source = (
            "# План\n\n"
            "**Важно:** сделать *сегодня*.\n"
            "- первый пункт\n"
            "- второй пункт\n\n"
            "`inline <code>`\n\n"
            "```python\n"
            'print("<hello>")\n'
            "```\n\n"
            "[OpenAI](https://openai.com/?a=1&b=2)\n"
            "> заметка\n"
            "~~старое~~ ||секрет||"
        )
        rendered = markdown_to_telegram_html(source)

        assert "<b>План</b>" in rendered
        assert "<b>Важно:</b>" in rendered
        assert "<i>сегодня</i>" in rendered
        assert "• первый пункт" in rendered
        assert "• второй пункт" in rendered
        assert "<code>inline &lt;code&gt;</code>" in rendered
        assert (
            '<pre><code class="language-python">'
            'print("&lt;hello&gt;")\n'
            "</code></pre>"
        ) in rendered
        assert '<a href="https://openai.com/?a=1&amp;b=2">OpenAI</a>' in rendered
        assert "<blockquote>заметка</blockquote>" in rendered
        assert "<s>старое</s>" in rendered
        assert "<tg-spoiler>секрет</tg-spoiler>" in rendered

    def test_raw_html_is_escaped(self) -> None:
        rendered = markdown_to_telegram_html(
            '<b>model html</b> & <script>alert("x")</script>'
        )
        assert "<script>" not in rendered
        assert "<b>model html</b>" not in rendered
        assert "&lt;b&gt;model html&lt;/b&gt;" in rendered
        assert "&amp;" in rendered

    def test_snake_case_is_not_italic(self) -> None:
        rendered = markdown_to_telegram_html(
            "Use snake_case and my_file_name.txt, but _this is italic_."
        )
        assert "snake_case" in rendered
        assert "my_file_name.txt" in rendered
        assert "<i>this is italic</i>" in rendered

    def test_unsafe_link_becomes_plain_text(self) -> None:
        rendered = markdown_to_telegram_html("[click](javascript:alert)")
        assert "<a " not in rendered
        assert "click (javascript:alert)" in rendered


class TestAnswerLongMarkdown:
    async def test_html_parse_mode_and_keyboard_on_last_segment(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        async def _answer(text: str, **kwargs: object) -> object:
            calls.append((text, dict(kwargs)))
            return object()

        message = SimpleNamespace(answer=_answer)
        markup = object()
        source = "**hello**\n\n" + ("x" * 120)

        await answer_long_markdown(
            message,
            source,
            reply_markup=markup,
            limit=60,
        )

        assert len(calls) >= 2
        assert all(kwargs["parse_mode"] == "HTML" for _, kwargs in calls)
        assert calls[0][1].get("reply_markup") is None
        assert calls[-1][1]["reply_markup"] is markup
        assert "<b>hello</b>" in calls[0][0]

    async def test_bad_html_falls_back_to_original_plain_segment(self) -> None:
        error = TelegramBadRequest(
            method=SendMessage(chat_id=1, text="x"),
            message="Bad Request: can't parse entities",
        )
        answer = AsyncMock(side_effect=[error, object()])
        message = SimpleNamespace(answer=answer)

        await answer_long_markdown(message, "**hello**")

        assert answer.await_count == 2
        formatted = answer.await_args_list[0]
        assert formatted.args[0] == "<b>hello</b>"
        assert formatted.kwargs["parse_mode"] == "HTML"

        fallback = answer.await_args_list[1]
        assert fallback.args[0] == "**hello**"
        assert "parse_mode" not in fallback.kwargs
