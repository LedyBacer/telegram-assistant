"""add lease expiry to background_jobs (real leases, SPEC §5.2)

Revision ID: a1b2c3d4e5f6
Revises: e8a2c41b7f05
Create Date: 2026-09-23 00:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "e8a2c41b7f05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A running job is abandoned only when this expires (lease heartbeat),
    # replacing the old "locked_at older than a fixed TTL" heuristic.
    op.add_column(
        "background_jobs",
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_background_jobs_lease",
        "background_jobs",
        ["status", "lease_until"],
    )


def downgrade() -> None:
    op.drop_index("ix_background_jobs_lease", table_name="background_jobs")
    op.drop_column("background_jobs", "lease_until")
