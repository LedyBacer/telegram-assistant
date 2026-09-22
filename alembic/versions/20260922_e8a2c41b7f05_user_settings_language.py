"""add per-user language to user_settings (i18n)

Revision ID: e8a2c41b7f05
Revises: 7b492f548c86
Create Date: 2026-09-22 12:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e8a2c41b7f05'
down_revision: str | Sequence[str] | None = '7b492f548c86'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the non-null per-user language column.

    The server default is 'ru' (assistant.i18n.DEFAULT_LANGUAGE), so every
    existing row migrates safely to Russian without a backfill pass.
    """
    op.add_column(
        "user_settings",
        sa.Column("language", sa.String(length=16), server_default="ru", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "language")
