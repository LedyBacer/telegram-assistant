"""Personal files and embedded chunks (SPEC §11, §12)."""

import enum
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from assistant.db.base import Base

EMBEDDING_DIMENSIONS = 1536


class FileState(enum.StrEnum):
    queued = "queued"
    downloading = "downloading"
    extracting = "extracting"
    chunking = "chunking"
    embedding = "embedding"
    indexed = "indexed"
    failed = "failed"
    rejected = "rejected"


class UserFile(Base):
    __tablename__ = "user_files"
    __table_args__ = (
        CheckConstraint(
            "state IN ('queued','downloading','extracting','chunking','embedding',"
            "'indexed','failed','rejected')",
            name="valid_state",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Stable internal storage identifier (not the user-supplied filename).
    storage_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    telegram_file_id: Mapped[str | None] = mapped_column(String(255), default=None)
    telegram_file_unique_id: Mapped[str | None] = mapped_column(String(255), default=None)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, default=None)
    state: Mapped[str] = mapped_column(
        Enum(FileState, values_callable=lambda e: [m.value for m in e]),
        default=FileState.queued.value,
        server_default="queued",
    )
    error: Mapped[str | None] = mapped_column(String(1000), default=None)
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("background_jobs.id", ondelete="SET NULL"), default=None
    )
    extra: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    user: Mapped["User"] = relationship(back_populates="files")
    chunks: Mapped[list["FileChunk"]] = relationship(
        back_populates="file", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<UserFile id={self.id} name={self.original_filename!r} state={self.state}>"


class FileChunk(Base):
    """A text chunk with its embedding vector. HNSW cosine index added in the
    initial migration (autogenerate cannot express it)."""

    __tablename__ = "file_chunks"
    __table_args__ = (Index("ix_file_chunks_user", "user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("user_files.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    file: Mapped["UserFile"] = relationship(back_populates="chunks")

    def __repr__(self) -> str:
        return f"<FileChunk id={self.id} file_id={self.file_id} pos={self.position}>"


from assistant.models.users import User  # noqa: E402
