"""SQLAlchemy ORM models.

Importing this package registers every table on the shared ``Base`` so that
Alembic autogenerate and ``create_all`` see the full schema.
"""

from assistant.db.base import Base
from assistant.models.calendar_items import (
    CalendarItem,
    ItemKind,
    ItemPriority,
    ItemStatus,
)
from assistant.models.chat_messages import ChatMessage
from assistant.models.digests import DigestDelivery
from assistant.models.facts import UserFact
from assistant.models.files import FileChunk, UserFile
from assistant.models.jobs import BackgroundJob
from assistant.models.pending_actions import ActionStatus, PendingAction
from assistant.models.proactivity import NudgeDelivery, NudgeKind, ProactiveSettings
from assistant.models.reminders import Reminder
from assistant.models.users import User, UserSettings
from assistant.models.workout_logs import WorkoutLog

__all__ = [
    "Base",
    "BackgroundJob",
    "CalendarItem",
    "ChatMessage",
    "DigestDelivery",
    "FileChunk",
    "ItemKind",
    "ItemPriority",
    "ItemStatus",
    "NudgeDelivery",
    "NudgeKind",
    "PendingAction",
    "ProactiveSettings",
    "Reminder",
    "ActionStatus",
    "User",
    "UserFact",
    "UserFile",
    "UserSettings",
    "WorkoutLog",
]
