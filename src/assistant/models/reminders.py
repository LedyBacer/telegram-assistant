"""Durable reminders (SPEC §8).

A reminder is persisted independently of the in-process scheduler; delivery is
performed by a durable background job so a worker restart cannot lose it.
``sent_at`` / ``cancelled_at`` make sends idempotent.
"""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from assistant.db.base import Base


class ReminderStatus(enum.StrEnum):
    pending = "pending"
    sent = "sent"
    cancelled = "cancelled"
    failed = "failed"


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('absolute', 'item_linked')", name="valid_trigger_type"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    calendar_item_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("calendar_items.id", ondelete="CASCADE"),
        default=None,
    )
    trigger_type: Mapped[str] = mapped_column(
        String(16), default="absolute", server_default="absolute"
    )
    # Minutes before the linked item's starts_at (only for item_linked).
    offset_minutes: Mapped[int | None] = mapped_column(default=None)
    # Effective fire time (absolute trigger, or resolved for item_linked).
    fire_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(ReminderStatus, values_callable=lambda e: [m.value for m in e]),
        default=ReminderStatus.pending.value,
        server_default="pending",
    )
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("background_jobs.id", ondelete="SET NULL"), default=None
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(1000), default=None)
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    user: Mapped["User"] = relationship(back_populates="reminders")
    calendar_item: Mapped["CalendarItem | None"] = relationship(
        back_populates="reminders"
    )

    def __repr__(self) -> str:
        return f"<Reminder id={self.id} fire_at={self.fire_at} status={self.status}>"


from assistant.models.calendar_items import CalendarItem  # noqa: E402
from assistant.models.users import User  # noqa: E402
