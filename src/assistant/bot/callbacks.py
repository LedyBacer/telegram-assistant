"""Callback data factories for the bot (aiogram 3)."""

from aiogram.filters.callback_data import CallbackData


class MenuCallback(CallbackData, prefix="menu"):
    """One level of inline navigation, no deep nesting (SPEC §5)."""

    section: str


class SettingsCallback(CallbackData, prefix="settings"):
    action: str


class LanguageCallback(CallbackData, prefix="lang"):
    """Select the user's interface language (persisted per user)."""

    code: str


class ItemCallback(CallbackData, prefix="item"):
    action: str
    item_id: int


class DraftCallback(CallbackData, prefix="draft"):
    """Confirm/cancel a structured draft before it is persisted (SPEC §6)."""

    action: str


class FactCallback(CallbackData, prefix="fact"):
    """Confirm/reject/delete a user fact (SPEC §14)."""

    action: str
    fact_id: int
