"""Inline keyboards for the persistent main menu (SPEC §5).

Every label goes through the central i18n translator in the caller's
language; no user-visible string is hard-coded here.
"""

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from assistant.bot.callbacks import (
    DraftCallback,
    FactCallback,
    ItemCallback,
    LanguageCallback,
    MenuCallback,
    SettingsCallback,
)
from assistant.i18n import SUPPORTED_LANGUAGES, t
from assistant.models.calendar_items import CalendarItem

# (section, translation key) — one flat level, no nested sub-menus (SPEC §5).
SECTIONS: tuple[tuple[str, str], ...] = (
    ("task", "menu.tasks"),
    ("today", "menu.today"),
    ("upcoming", "menu.upcoming"),
    ("workouts", "menu.workouts"),
    ("files", "menu.files"),
    ("ask", "menu.ask"),
    ("miniapp", "menu.miniapp"),
    ("settings", "menu.settings"),
)


def main_menu_kb(language: str, base_url: str | None = None) -> InlineKeyboardMarkup:
    """The persistent main menu shown after every bot answer."""
    buttons: list[InlineKeyboardButton] = []
    for section, key in SECTIONS:
        if section == "miniapp":
            if not base_url:
                continue
            buttons.append(
                InlineKeyboardButton(
                    text=t(language, key), web_app=WebAppInfo(url=base_url)
                )
            )
        else:
            buttons.append(
                InlineKeyboardButton(
                    text=t(language, key),
                    callback_data=MenuCallback(section=section).pack(),
                )
            )
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb(language: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "settings.timezone"),
                    callback_data=SettingsCallback(action="timezone").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "settings.digest_time"),
                    callback_data=SettingsCallback(action="digest_time").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "settings.language"),
                    callback_data=SettingsCallback(action="language").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "menu.back"),
                    callback_data=MenuCallback(section="main").pack(),
                )
            ],
        ]
    )


def language_kb(language: str) -> InlineKeyboardMarkup:
    """One button per supported language (driven by the language registry)."""
    rows = [
        [
            InlineKeyboardButton(
                text=t(language, f"settings.language_{code}"),
                callback_data=LanguageCallback(code=code).pack(),
            )
        ]
        for code in sorted(SUPPORTED_LANGUAGES)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def draft_kb(language: str) -> InlineKeyboardMarkup:
    """Explicit human confirmation before a draft is persisted (SPEC §6)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "draft.confirm"),
                    callback_data=DraftCallback(action="confirm").pack(),
                ),
                InlineKeyboardButton(
                    text=t(language, "draft.cancel"),
                    callback_data=DraftCallback(action="cancel").pack(),
                ),
            ]
        ]
    )


def workouts_kb(language: str) -> InlineKeyboardMarkup:
    """Workout section actions (SPEC §10)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(language, "menu.log_workout"),
                    callback_data=MenuCallback(section="log_workout").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "menu.schedule_workout"),
                    callback_data=MenuCallback(section="schedule_workout").pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t(language, "menu.back"),
                    callback_data=MenuCallback(section="main").pack(),
                )
            ],
        ]
    )


def fact_kb(fact_id: int, *, confirmable: bool, language: str) -> InlineKeyboardMarkup:
    """Fact actions: confirm/reject for proposed facts, delete for all
    (SPEC §14)."""
    rows: list[list[InlineKeyboardButton]] = []
    if confirmable:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(language, "fact.confirm"),
                    callback_data=FactCallback(action="confirm", fact_id=fact_id).pack(),
                ),
                InlineKeyboardButton(
                    text=t(language, "fact.reject"),
                    callback_data=FactCallback(action="reject", fact_id=fact_id).pack(),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text=t(language, "fact.delete"),
                callback_data=FactCallback(action="delete", fact_id=fact_id).pack(),
            ),
            InlineKeyboardButton(
                text=t(language, "menu.back"),
                callback_data=MenuCallback(section="main").pack(),
            ),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def items_kb(items: Sequence[CalendarItem], language: str) -> InlineKeyboardMarkup:
    """Per-item complete/cancel buttons for a calendar item list."""
    rows = [
        [
            InlineKeyboardButton(
                text=f"✅ {item.title[:40]}",
                callback_data=ItemCallback(action="complete", item_id=item.id).pack(),
            ),
            InlineKeyboardButton(
                text=t(language, "item.cancel"),
                callback_data=ItemCallback(action="cancel", item_id=item.id).pack(),
            ),
        ]
        for item in items
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=t(language, "menu.back"),
                callback_data=MenuCallback(section="main").pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
