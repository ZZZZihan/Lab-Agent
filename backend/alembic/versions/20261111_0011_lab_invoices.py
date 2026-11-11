"""Add lab_invoices table for PDF invoice management and auto-renaming."""

from __future__ import annotations

from alembic import op

revision = "20261111_0011"
down_revision = "20261111_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE lab_invoices (
            id UUID PRIMARY KEY,
            owner_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            original_filename VARCHAR(255) NOT NULL,
            item_name VARCHAR(200) NOT NULL,
            raw_item_name VARCHAR(300),
            total_amount NUMERIC(12, 2) NOT NULL,
            invoice_date DATE,
            invoice_number VARCHAR(64),
            note VARCHAR(500),
            pdf_data BYTEA NOT NULL,
            pdf_sha256 VARCHAR(64) NOT NULL,
            pdf_size_bytes INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_lab_invoices_total_amount CHECK (
                total_amount >= 0 AND total_amount <= 9999999999.99
            ),
            CONSTRAINT ck_lab_invoices_pdf_size CHECK (
                octet_length(pdf_data) > 0 AND octet_length(pdf_data) <= 10485760
            ),
            CONSTRAINT ck_lab_invoices_pdf_size_col CHECK (
                pdf_size_bytes > 0 AND pdf_size_bytes <= 10485760
            ),
            CONSTRAINT ck_lab_invoices_pdf_sha256 CHECK (
                pdf_sha256 ~ '^[0-9a-f]{64}$'
            )
        );
        """
    )
    op.execute("CREATE INDEX lab_invoices_owner_idx ON lab_invoices (owner_id);")
    op.execute("CREATE INDEX lab_invoices_date_idx ON lab_invoices (invoice_date);")
    op.execute("CREATE INDEX lab_invoices_sha256_idx ON lab_invoices (pdf_sha256);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lab_invoices;")
