"""Add ingestion status, source_format, content_sha256, error_code, attempts, and updated_at."""

from __future__ import annotations

from alembic import op

revision = "20261106_0003"
down_revision = "20261105_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE documents
          ADD COLUMN source_format varchar(16) NOT NULL DEFAULT 'markdown',
          ADD COLUMN original_filename varchar(255),
          ADD COLUMN content_sha256 char(64),
          ADD COLUMN error_code varchar(64),
          ADD COLUMN attempts integer NOT NULL DEFAULT 0,
          ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
        """
    )
    op.execute("ALTER TABLE documents DROP CONSTRAINT documents_status;")
    op.execute(
        """
        ALTER TABLE documents ADD CONSTRAINT documents_status
          CHECK (status IN ('pending','processing','ready','failed'));
        """
    )
    op.execute(
        """
        ALTER TABLE documents ADD CONSTRAINT documents_source_format
          CHECK (source_format IN ('markdown','text','pdf'));
        """
    )
    op.execute(
        """
        CREATE INDEX documents_pending_idx ON documents (created_at)
          WHERE status = 'pending' AND deleted_at IS NULL;
        """
    )
    op.execute(
        """
        UPDATE documents
          SET content_sha256 = encode(sha256(convert_to(content_text,'UTF8')),'hex');
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS documents_pending_idx;")
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_source_format;")
    op.execute("ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_status;")
    op.execute("UPDATE documents SET status = 'pending' WHERE status = 'processing';")
    op.execute(
        """
        ALTER TABLE documents ADD CONSTRAINT documents_status
          CHECK (status IN ('pending','ready','failed'));
        """
    )
    op.execute(
        """
        ALTER TABLE documents
          DROP COLUMN IF EXISTS updated_at,
          DROP COLUMN IF EXISTS attempts,
          DROP COLUMN IF EXISTS error_code,
          DROP COLUMN IF EXISTS content_sha256,
          DROP COLUMN IF EXISTS original_filename,
          DROP COLUMN IF EXISTS source_format;
        """
    )
