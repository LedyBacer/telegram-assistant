"""Lightweight, state-derived motivational messages (SPEC §17).

Deterministic (no AI call) and spam-safe: at most one line, only when the
user has not disabled motivation, and derived from real application state.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    """Return one short line based on real state, or None to stay silent."""
    if user.settings is not None and not user.settings.motivation_enabled:
        return None
    if state.has_overdue:
        return (
            "You have overdue items — even a ten-minute push today keeps "
            "the momentum alive."
        )
    if state.has_upcoming_workout:
        return "Workout on the way — a bit of rest now is fuel for later."
    if state.current_streak >= _MIN_STREAK_FOR_PRAISE:
        return (
            f"{state.current_streak}-day streak. Consistency compounds; "
            "keep it alive."
        )
    if state.schedule_empty:
        return "Your schedule is clear today — a good time to plan one small win."
    return "One thing done is better than ten planned."
