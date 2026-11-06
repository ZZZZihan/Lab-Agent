"""Add electronic signature columns and constraints to lab_people (Spec v2.2 §2)."""

from __future__ import annotations

from alembic import op

revision = "20261110_0009"
down_revision = "20261110_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE lab_people
          ADD COLUMN signature_data BYTEA NULL,
          ADD COLUMN signature_mime VARCHAR(32) NULL,
          ADD COLUMN signature_sha256 VARCHAR(64) NULL,
          ADD COLUMN signature_updated_at TIMESTAMPTZ NULL,
          ADD COLUMN signature_source VARCHAR(16) NULL;
        """
    )
    op.execute(
        """
        ALTER TABLE lab_people
          ADD CONSTRAINT ck_lab_people_signature_size CHECK (
            signature_data IS NULL
            OR (octet_length(signature_data) > 0 AND octet_length(signature_data) <= 2097152)
          ),
          ADD CONSTRAINT ck_lab_people_signature_mime CHECK (
            signature_mime IS NULL
            OR signature_mime IN ('image/png', 'image/jpeg', 'image/webp')
          ),
          ADD CONSTRAINT ck_lab_people_signature_sha256 CHECK (
            signature_sha256 IS NULL
            OR signature_sha256 ~ '^[0-9a-f]{64}$'
          ),
          ADD CONSTRAINT ck_lab_people_signature_source CHECK (
            signature_source IS NULL
            OR signature_source IN ('docx_import', 'upload')
          ),
          ADD CONSTRAINT ck_lab_people_signature_all_or_none CHECK (
            (
              signature_data IS NULL
              AND signature_mime IS NULL
              AND signature_sha256 IS NULL
              AND signature_updated_at IS NULL
              AND signature_source IS NULL
            )
            OR (
              signature_data IS NOT NULL
              AND signature_mime IS NOT NULL
              AND signature_sha256 IS NOT NULL
              AND signature_updated_at IS NOT NULL
              AND signature_source IS NOT NULL
            )
          );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE lab_people
          DROP CONSTRAINT IF EXISTS ck_lab_people_signature_all_or_none,
          DROP CONSTRAINT IF EXISTS ck_lab_people_signature_source,
          DROP CONSTRAINT IF EXISTS ck_lab_people_signature_sha256,
          DROP CONSTRAINT IF EXISTS ck_lab_people_signature_mime,
          DROP CONSTRAINT IF EXISTS ck_lab_people_signature_size;
        """
    )
    op.execute(
        """
        ALTER TABLE lab_people
          DROP COLUMN IF EXISTS signature_source,
          DROP COLUMN IF EXISTS signature_updated_at,
          DROP COLUMN IF EXISTS signature_sha256,
          DROP COLUMN IF EXISTS signature_mime,
          DROP COLUMN IF EXISTS signature_data;
        """
    )
