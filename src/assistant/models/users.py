"""User identity and per-user settings."""

from datetime import datetime, time

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Time, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from assistant.db.base import Base
from assistant.i18n import DEFAULT_LANGUAGE


class User(Base):
    """A bot user. Telegram user ID is the primary external identity."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255), default=None)
    username: Mapped[str | None] = mapped_column(String(255), default=None)
    is_bot: Mapped[bool] = mapped_column(default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    settings: Mapped["UserSettings | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    calendar_items: Mapped[list["CalendarItem"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    workouts: Mapped[list["WorkoutLog"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    files: Mapped[list["UserFile"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    facts: Mapped[list["UserFact"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"


class UserSettings(Base):
    """Per-user preferences: timezone, digest time, motivation toggles."""

    __tablename__ = "user_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    timezone: Mapped[str] = mapped_column(
        String(64), default="UTC", server_default="UTC"
    )
    digest_time: Mapped[time] = mapped_column(
        Time, default=time(8, 0), server_default="08:00:00"
    )
    motivation_enabled: Mapped[bool] = mapped_column(
        default=True, server_default="true"
    )
    language: Mapped[str] = mapped_column(
        String(16), default=DEFAULT_LANGUAGE, server_default=DEFAULT_LANGUAGE
    )
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="settings")

    def __repr__(self) -> str:
        return f"<UserSettings user_id={self.user_id} tz={self.timezone}>"


from assistant.models.calendar_items import CalendarItem  # noqa: E402
from assistant.models.chat_messages import ChatMessage  # noqa: E402
from assistant.models.facts import UserFact  # noqa: E402
from assistant.models.files import UserFile  # noqa: E402
from assistant.models.reminders import Reminder  # noqa: E402
from assistant.models.workout_logs import WorkoutLog  # noqa: E402
