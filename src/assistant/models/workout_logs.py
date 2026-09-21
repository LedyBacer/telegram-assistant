"""Workout logs (SPEC §10)."""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from assistant.db.base import Base


class WorkoutStatus(enum.StrEnum):
    planned = "planned"
    completed = "completed"
    skipped = "skipped"
    cancelled = "cancelled"


class WorkoutLog(Base):
    __tablename__ = "workout_logs"
    __table_args__ = (Index("ix_workout_logs_user_started", "user_id", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_minutes: Mapped[int | None] = mapped_column(Integer, default=None)
    notes: Mapped[str | None] = mapped_column(String(4000), default=None)
    # Perceived effort 1 (easy) .. 10 (exhausting), optional.
    perceived_effort: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(
        Enum(WorkoutStatus, values_callable=lambda e: [m.value for m in e]),
        default=WorkoutStatus.completed.value,
        server_default="completed",
    )
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="workouts")

    def __repr__(self) -> str:
        return f"<WorkoutLog id={self.id} name={self.name!r} at={self.started_at}>"


from assistant.models.users import User  # noqa: E402
