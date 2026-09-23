"""User facts / long-term memory (SPEC §14)."""

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from assistant.db.base import Base


class FactStatus(enum.StrEnum):
    proposed = "proposed"
    confirmed = "confirmed"
    rejected = "rejected"
    superseded = "superseded"


class UserFact(Base):
    __tablename__ = "user_facts"
    __table_args__ = (
        Index("ix_user_facts_user_status", "user_id", "status", "category"),
        Index("ix_user_facts_user_key_hash", "user_id", "key_hash"),
        CheckConstraint("confidence BETWEEN 0 AND 1", name="valid_confidence"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(64), nullable=False, default="general")
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Collision-resistant dedupe identity: a stable digest of the normalized
    # full value (SPEC §16). ``key`` is a human-readable truncated form used
    # for display only; dedupe compares this hash so two distinct facts that
    # share a 255-char prefix can never collide.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str | None] = mapped_column(String(255), default=None)
    confidence: Mapped[float | None] = mapped_column(Numeric(3, 2), default=None)
    status: Mapped[str] = mapped_column(
        Enum(FactStatus, values_callable=lambda e: [m.value for m in e]),
        default=FactStatus.proposed.value,
        server_default="proposed",
    )
    superseded_by: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # Explicit replacement link (SPEC §15): the fact this one proposes to
    # replace. SET NULL so deleting the referenced fact never cascades onto
    # the replacement.
    replaces_fact_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("user_facts.id", ondelete="SET NULL"),
        default=None,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(back_populates="facts")

    def __repr__(self) -> str:
        return f"<UserFact id={self.id} key={self.key!r} status={self.status}>"


from assistant.models.users import User  # noqa: E402
