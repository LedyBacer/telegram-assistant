"""add pending_actions (durable confirmed mutations, SPEC §3)

Revision ID: f7a8b9c0d1e2
Revises: a1b2c3d4e5f6
Create Date: 2026-09-23 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_actions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "proposed",
                "confirmed",
                "rejected",
                "executed",
                "expired",
                name="actionstatus",
            ),
            server_default="proposed",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_result",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("last_error", sa.String(1000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_pending_actions_user_status", "pending_actions", ["user_id", "status"]
    )
    op.create_index(
        "ix_pending_actions_expires", "pending_actions", ["status", "expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_pending_actions_expires", table_name="pending_actions")
    op.drop_index("ix_pending_actions_user_status", table_name="pending_actions")
    op.drop_table("pending_actions")
