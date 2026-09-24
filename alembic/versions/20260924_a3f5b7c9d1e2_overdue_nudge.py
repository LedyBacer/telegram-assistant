"""overdue-task nudge toggle (V3 P38)

Adds ``proactive_settings.overdue_nudge_enabled``: the user-facing switch
for the deterministic daily overdue-task nudge. On by default, matching
the existing weekly-review and workout nudge defaults.

Revision ID: a3f5b7c9d1e2
Revises: b7c8d9e0f1b3
Create Date: 2026-09-24 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f5b7c9d1e2"
down_revision: str | Sequence[str] | None = "b7c8d9e0f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proactive_settings",
        sa.Column(
            "overdue_nudge_enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("proactive_settings", "overdue_nudge_enabled")
