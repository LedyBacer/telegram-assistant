"""Lightweight, state-derived motivational messages (SPEC §17).

Deterministic (no AI call) and spam-safe: at most one line, only when the
user has not disabled motivation, and derived from real application state.
"""

from __future__ import annotations

from dataclasses import dataclass

from assistant.i18n import DEFAULT_LANGUAGE, t
from assistant.models.users import User

_MIN_STREAK_FOR_PRAISE = 2


@dataclass(slots=True)
class MotivationState:
    """Relevant slices of application state for a motivational line."""

    has_overdue: bool
    has_upcoming_workout: bool
    current_streak: int
    schedule_empty: bool


def motivational_line(user: User, state: MotivationState) -> str | None:
    """Return one short line based on real state, or None to stay silent.

    Rendered in the user's persisted language at call time.
    """
    if user.settings is not None and not user.settings.motivation_enabled:
        return None
    language = user.settings.language if user.settings is not None else DEFAULT_LANGUAGE
    if state.has_overdue:
        return t(language, "motivation.overdue")
    if state.has_upcoming_workout:
        return t(language, "motivation.workout")
    if state.current_streak >= _MIN_STREAK_FOR_PRAISE:
        return t(language, "motivation.streak", streak=state.current_streak)
    if state.schedule_empty:
        return t(language, "motivation.empty")
    return t(language, "motivation.default")
