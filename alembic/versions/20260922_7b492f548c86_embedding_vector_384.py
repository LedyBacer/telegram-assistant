"""shrink file_chunks.embedding from vector(1536) to vector(384)

Revision ID: 7b492f548c86
Revises: c390315de59f
Create Date: 2026-09-22 10:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = '7b492f548c86'
down_revision: str | Sequence[str] | None = 'c390315de59f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Change file_chunks.embedding from vector(1536) to vector(384).

    pgvector has no cast between vector dimensions, so the column is dropped
    and recreated. The application is pre-production: existing vectors are
    discarded (files can be re-ingested on demand). The HNSW cosine index is
    rebuilt on the new column.
    """
    op.execute("DROP INDEX IF EXISTS ix_file_chunks_embedding_hnsw")
    with op.batch_alter_table("file_chunks") as batch:
        batch.drop_column("embedding")
        batch.add_column(sa.Column("embedding", Vector(dim=384), nullable=True))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_file_chunks_embedding_hnsw "
        "ON file_chunks USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Restore the original vector(1536) column and HNSW index."""
    op.execute("DROP INDEX IF EXISTS ix_file_chunks_embedding_hnsw")
    with op.batch_alter_table("file_chunks") as batch:
        batch.drop_column("embedding")
        batch.add_column(sa.Column("embedding", Vector(dim=1536), nullable=True))
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_file_chunks_embedding_hnsw "
        "ON file_chunks USING hnsw (embedding vector_cosine_ops)"
    )
