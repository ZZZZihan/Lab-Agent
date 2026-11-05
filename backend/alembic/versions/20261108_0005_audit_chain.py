"""Add prev_hash and row_hash columns to audit_events for tamper-evident hash chaining."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261108_0005"
down_revision = "20261107_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("prev_hash", sa.CHAR(64), nullable=True))
    op.add_column("audit_events", sa.Column("row_hash", sa.CHAR(64), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_events", "row_hash")
    op.drop_column("audit_events", "prev_hash")
