"""Inline keyboards for the persistent main menu (SPEC §5)."""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from assistant.bot.callbacks import (
    DraftCallback,
    ItemCallback,
    MenuCallback,
    SettingsCallback,
)
from assistant.models.calendar_items import CalendarItem

# (section, label) — one flat level, no nested sub-menus (SPEC §5).
SECTIONS: tuple[tuple[str, str], ...] = (
    ("task", "➕ Add task / event"),
    ("today", "📅 Today's plan"),
    ("upcoming", "📆 Upcoming (7 days)"),
    ("workouts", "💪 Workouts"),
    ("files", "📁 My files"),
    ("ask", "💬 Ask assistant"),
    ("miniapp", "🧩 Open Mini App"),
    ("settings", "⚙️ Settings / help"),
)


def main_menu_kb(base_url: str | None = None) -> InlineKeyboardMarkup:
    """The persistent main menu shown after every bot answer."""
    buttons: list[InlineKeyboardButton] = []
    for section, label in SECTIONS:
        if section == "miniapp":
            if not base_url:
                continue
            buttons.append(
                InlineKeyboardButton(text=label, web_app=WebAppInfo(url=base_url))
            )
        else:
            buttons.append(
                InlineKeyboardButton(
                    text=label, callback_data=MenuCallback(section=section).pack()
                )
            )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌍 Change timezone",
                    callback_data=SettingsCallback(action="timezone").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏰ Change digest time",
                    callback_data=SettingsCallback(action="digest_time").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Main menu",
                    callback_data=MenuCallback(section="main").pack(),
                )
            ],
        ]
    )


def draft_kb() -> InlineKeyboardMarkup:
    """Explicit human confirmation before a draft is persisted (SPEC §6)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Confirm",
                    callback_data=DraftCallback(action="confirm").pack(),
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data=DraftCallback(action="cancel").pack(),
                ),
            ]
        ]
    )


def items_kb(items: Sequence[CalendarItem]) -> InlineKeyboardMarkup:
    """Per-item complete/cancel buttons for a calendar item list."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"✅ {item.title[:40]}",
                callback_data=ItemCallback(action="complete", item_id=item.id).pack(),
            ),
            InlineKeyboardButton(
                text="🚫",
                callback_data=ItemCallback(action="cancel", item_id=item.id).pack(),
            ),
        ]
        for item in items
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text="🏠 Main menu",
                callback_data=MenuCallback(section="main").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
