"""FSM states for multi-step bot conversations."""

from aiogram.fsm.state import State, StatesGroup


class TaskDraftStates(StatesGroup):
    """Natural-language task/event creation (SPEC §6)."""

    waiting_for_text = State()
    confirm = State()


class SettingsStates(StatesGroup):
    """Settings adjustments from the main menu."""

    digest_time = State()
    timezone = State()
