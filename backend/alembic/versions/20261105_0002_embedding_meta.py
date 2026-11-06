"""Create embedding_index_meta single-row table."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261105_0002"
down_revision = "20261104_0001a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "embedding_index_meta",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("embedder", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("storage", sa.Text(), nullable=False),
        sa.Column("index_kind", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("id = 1", name="embedding_index_meta_id"),
        sa.CheckConstraint(
            "storage IN ('vector', 'halfvec')",
            name="embedding_index_meta_storage",
        ),
        sa.CheckConstraint(
            "index_kind IN ('hnsw', 'hnsw_prefix', 'none')",
            name="embedding_index_meta_index_kind",
        ),
    )


def downgrade() -> None:
    op.drop_table("embedding_index_meta")
