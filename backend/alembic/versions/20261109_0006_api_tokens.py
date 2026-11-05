"""Add api_tokens table for Personal Access Tokens (la_pat_)."""

from __future__ import annotations

from alembic import op

revision = "20261109_0006"
down_revision = "20261108_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE api_tokens (
          id uuid PRIMARY KEY,
          user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          name varchar(120) NOT NULL,
          token_hash varchar(64) UNIQUE NOT NULL,
          prefix varchar(24) NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          last_used_at timestamptz NULL,
          expires_at timestamptz NULL,
          revoked_at timestamptz NULL
        );
        """
    )
    op.execute("CREATE INDEX api_tokens_user_idx ON api_tokens(user_id);")
    op.execute("CREATE INDEX api_tokens_hash_idx ON api_tokens(token_hash);")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS api_tokens_hash_idx;")
    op.execute("DROP INDEX IF EXISTS api_tokens_user_idx;")
    op.execute("DROP TABLE IF EXISTS api_tokens;")
