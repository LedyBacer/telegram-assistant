"""explicit fact replacement link (SPEC §15)

Revision ID: b7c8d9e0f1a2
Revises: 9c8d7e6f5a4b
Create Date: 2026-09-23 09:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "9c8d7e6f5a4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_facts",
        sa.Column("replaces_fact_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_facts_replaces_user_facts",
        "user_facts",
        "user_facts",
        ["replaces_fact_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_user_facts_replaces_user_facts", "user_facts", type_="foreignkey"
    )
    op.drop_column("user_facts", "replaces_fact_id")
