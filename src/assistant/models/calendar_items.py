"""Calendar items: tasks and events (SPEC §7)."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from assistant.db.base import Base


class ItemKind(enum.StrEnum):
    task = "task"
    event = "event"


class ItemStatus(enum.StrEnum):
    scheduled = "scheduled"
    completed = "completed"
    cancelled = "cancelled"


class ItemPriority(enum.StrEnum):
    low = "low"
    normal = "normal"
    high = "high"


class CalendarItem(Base):
    __tablename__ = "calendar_items"
    __table_args__ = (
        Index("ix_calendar_items_user_start", "user_id", "starts_at"),
        Index("ix_calendar_items_user_status", "user_id", "status", "starts_at"),
        CheckConstraint("priority IN ('low', 'normal', 'high')", name="valid_priority"),
        CheckConstraint("kind IN ('task', 'event')", name="valid_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(
        Enum(ItemKind, values_callable=lambda e: [m.value for m in e]),
        default=ItemKind.task.value,
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4000), default=None)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    status: Mapped[str] = mapped_column(
        Enum(ItemStatus, values_callable=lambda e: [m.value for m in e]),
        default=ItemStatus.scheduled.value,
        server_default="scheduled",
    )
    priority: Mapped[str] = mapped_column(
        Enum(ItemPriority, values_callable=lambda e: [m.value for m in e]),
        default=ItemPriority.normal.value,
        server_default="normal",
    )
    source: Mapped[str] = mapped_column(String(32), default="bot", server_default="bot")
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    user: Mapped["User"] = relationship(back_populates="calendar_items")

    def __repr__(self) -> str:
        return f"<CalendarItem id={self.id} kind={self.kind} title={self.title!r}>"


from assistant.models.users import User  # noqa: E402  (avoid circular import)
