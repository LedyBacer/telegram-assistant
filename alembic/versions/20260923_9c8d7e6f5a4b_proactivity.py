"""add proactivity: per-user settings and nudge delivery dedupe

Revision ID: 9c8d7e6f5a4b
Revises: f7a8b9c0d1e2
Create Date: 2026-09-23 15:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c8d7e6f5a4b"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "proactive_settings",
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "quiet_hours_start", sa.Time(), server_default="22:00:00", nullable=False
        ),
        sa.Column(
            "quiet_hours_end", sa.Time(), server_default="08:00:00", nullable=False
        ),
        sa.Column(
            "max_nudges_per_day", sa.Integer(), server_default="3", nullable=False
        ),
        sa.Column(
            "min_interval_minutes", sa.Integer(), server_default="120", nullable=False
        ),
        sa.Column(
            "weekly_review_enabled", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column(
            "workout_nudge_enabled", sa.Boolean(), server_default="true", nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "nudge_deliveries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("period_key", sa.String(32), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "kind", "period_key", name="uq_nudges_user_kind_period"
        ),
    )
    op.create_index("ix_nudges_user_sent", "nudge_deliveries", ["user_id", "sent_at"])


def downgrade() -> None:
    op.drop_index("ix_nudges_user_sent", table_name="nudge_deliveries")
    op.drop_table("nudge_deliveries")
    op.drop_table("proactive_settings")
