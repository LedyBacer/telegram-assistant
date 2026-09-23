"""Durable pending actions (SPEC §3).

A mutation is never applied directly: the assistant proposes a typed action,
the user confirms it, and only then is it executed. The row is durable in
PostgreSQL, so a restart between proposal and execution loses nothing. The
``payload`` is stored as JSONB and re-validated against the kind's Pydantic
schema at execution time (SPEC §3).
"""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from assistant.db.base import Base


class ActionStatus(enum.StrEnum):
    proposed = "proposed"
    confirmed = "confirmed"
    rejected = "rejected"
    executed = "executed"
    expired = "expired"


class PendingAction(Base):
    __tablename__ = "pending_actions"
    __table_args__ = (
        Index("ix_pending_actions_user_status", "user_id", "status"),
        Index("ix_pending_actions_expires", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Registered action kind (see assistant.actions); determines the payload
    # schema and the executor applied at confirmation time.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    # Human-readable description of the proposed mutation (for the UI).
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Enum(ActionStatus, values_callable=lambda e: [m.value for m in e]),
        default=ActionStatus.proposed.value,
        server_default="proposed",
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # JSON-serializable result of execution (idempotent re-execution source).
    last_result: Mapped[dict | None] = mapped_column(JSONB, default=None)
    last_error: Mapped[str | None] = mapped_column(String(1000), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    expired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    user: Mapped["User"] = relationship(back_populates="pending_actions")

    def __repr__(self) -> str:
        return f"<PendingAction id={self.id} kind={self.kind!r} status={self.status}>"


from assistant.models.users import User  # noqa: E402
