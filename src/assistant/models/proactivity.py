"""Limited proactivity: per-user anti-spam settings and nudge deliveries.

Proactivity is deliberately bounded: a small set of deterministic,
state-derived triggers (weekly review, workout nudge) evaluated by the
worker, gated by per-user settings (enabled, quiet hours in the user's
timezone, max nudges per day, minimum interval), and deduplicated by a
durable ``NudgeDelivery`` row per (user, kind, period) so a worker restart
never double-sends.
"""

import enum
from datetime import datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from assistant.db.base import Base


class NudgeKind(enum.StrEnum):
    weekly_review = "weekly_review"
    workout = "workout"
    overdue = "overdue"


class ProactiveSettings(Base):
    """Per-user proactivity and anti-spam preferences."""

    __tablename__ = "proactive_settings"

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    # Quiet hours in the user's own timezone. A range may wrap midnight
    # (22:00 -> 08:00 is the default quiet window).
    quiet_hours_start: Mapped[time] = mapped_column(
        Time, default=time(22, 0), server_default="22:00:00"
    )
    quiet_hours_end: Mapped[time] = mapped_column(
        Time, default=time(8, 0), server_default="08:00:00"
    )
    max_nudges_per_day: Mapped[int] = mapped_column(
        Integer, default=3, server_default="3"
    )
    min_interval_minutes: Mapped[int] = mapped_column(
        Integer, default=120, server_default="120"
    )
    weekly_review_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    workout_nudge_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    overdue_nudge_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="proactive_settings")

    def __repr__(self) -> str:
        return f"<ProactiveSettings user_id={self.user_id} enabled={self.enabled}>"


class NudgeDelivery(Base):
    """Durable dedupe record: one row per (user, kind, period)."""

    __tablename__ = "nudge_deliveries"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "period_key", name="uq_nudges_user_kind_period"),
        Index("ix_nudges_user_sent", "user_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Period identifier for the dedupe window: an ISO week ("2026-W39") for
    # the weekly review, a local date ("2026-09-23") for the workout nudge.
    period_key: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<NudgeDelivery user_id={self.user_id} kind={self.kind!r} period={self.period_key!r}>"


from assistant.models.users import User  # noqa: E402
