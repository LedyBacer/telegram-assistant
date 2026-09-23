"""collision-resistant fact dedupe key (SPEC §16)

Adds ``user_facts.key_hash``: a SHA-256 digest of the normalized fact value
used as the dedupe identity, replacing reliance on the truncated 255-char
``key`` (which could collide for long facts sharing a prefix). Existing rows
are backfilled from their ``value``.

Revision ID: b7c8d9e0f1b3
Revises: b7c8d9e0f1a2
Create Date: 2026-09-24 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1b3"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize(value: str) -> str:
    return " ".join((value or "").split()).lower()


def _fact_hash(value: str) -> str:
    import hashlib

    return hashlib.sha256(_normalize(value).encode("utf-8")).hexdigest()


def upgrade() -> None:
    # Add nullable first so the backfill can run without a NOT NULL violation
    # (pgcrypto is not assumed, so the digest is computed in Python).
    op.add_column(
        "user_facts",
        sa.Column("key_hash", sa.String(length=64), nullable=True),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, value FROM user_facts")).all()
    for row in rows:
        bind.execute(
            sa.text("UPDATE user_facts SET key_hash = :h WHERE id = :id"),
            {"h": _fact_hash(row[1]), "id": row[0]},
        )
    op.alter_column(
        "user_facts",
        "key_hash",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_index("ix_user_facts_user_key_hash", "user_facts", ["user_id", "key_hash"])


def downgrade() -> None:
    op.drop_index("ix_user_facts_user_key_hash", table_name="user_facts")
    op.drop_column("user_facts", "key_hash")
