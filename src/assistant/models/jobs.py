"""Durable PostgreSQL background jobs (SPEC §9).

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` inside an explicit
transaction (see ``assistant.services.jobs``). ``idempotency_key`` gives a
unique-per-type guard so retries/restarts cannot double-execute a logical job.
"""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from assistant.db.base import Base


class JobStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class BackgroundJob(Base):
    __tablename__ = "background_jobs"
    __table_args__ = (
        Index("ix_background_jobs_claim", "status", "available_at"),
        Index("ix_background_jobs_user", "user_id"),
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="valid_status",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), default=None, unique=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default=JobStatus.pending.value, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(default=3, server_default="3")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Lease owner token (the worker that claimed the job) and the lease
    # expiry. A running job is considered abandoned ONLY when its lease has
    # expired (lease_until < now()); the owner renews the lease via heartbeat
    # while the job runs. Only the current owner may complete/fail/renew.
    locked_by: Mapped[str | None] = mapped_column(String(128), default=None)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
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

    def __repr__(self) -> str:
        return f"<BackgroundJob id={self.id} type={self.type} status={self.status}>"
