"""Daily digest delivery records (SPEC §16).

Unique per (user_id, digest_date) makes digest delivery idempotent across
worker restarts.
"""

from datetime import date, datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base


class DigestDelivery(Base):
    __tablename__ = "digests"
    __table_args__ = (
        UniqueConstraint("user_id", "digest_date", name="uq_digests_user_day"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    digest_date: Mapped[date] = mapped_column(nullable=False)
    content: Mapped[str | None] = mapped_column(Text, default=None)
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("background_jobs.id", ondelete="SET NULL"), default=None
    )
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    def __repr__(self) -> str:
        return f"<DigestDelivery user_id={self.user_id} date={self.digest_date}>"
