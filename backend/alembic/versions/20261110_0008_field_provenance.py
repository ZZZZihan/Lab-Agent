"""Add field_provenance table and support merged status on import_jobs (Spec §10.3)."""

from __future__ import annotations

from alembic import op

revision = "20261110_0008"
down_revision = "20261110_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE import_jobs DROP CONSTRAINT IF EXISTS import_jobs_status_check;")
    op.execute("ALTER TABLE import_jobs DROP CONSTRAINT IF EXISTS import_jobs_status;")
    op.execute(
        "ALTER TABLE import_jobs ADD CONSTRAINT import_jobs_status "
        "CHECK (status IN ('parsed', 'provisioned', 'merged', 'failed'));"
    )

    op.execute(
        """
        CREATE TABLE field_provenance (
          id bigserial PRIMARY KEY,
          entity_type varchar(32) NOT NULL
            CHECK (entity_type IN ('person', 'project', 'achievement', 'facility')),
          entity_id uuid NOT NULL,
          field varchar(64) NOT NULL,
          value jsonb NULL,
          source_kind varchar(16) NOT NULL
            CHECK (source_kind IN ('import', 'manual', 'legacy')),
          source_job_id uuid NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
          as_of_time date NOT NULL,
          recorded_at timestamptz NOT NULL DEFAULT now(),
          recorded_by uuid NULL REFERENCES users(id) ON DELETE SET NULL,
          superseded boolean NOT NULL DEFAULT false,
          CONSTRAINT field_provenance_import_job_check
            CHECK (source_kind <> 'import' OR source_job_id IS NOT NULL)
        );
        """
    )
    op.execute(
        "CREATE INDEX field_provenance_entity_field_idx "
        "ON field_provenance (entity_type, entity_id, field);"
    )
    op.execute(
        "CREATE INDEX field_provenance_job_idx "
        "ON field_provenance (source_job_id);"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS field_provenance_job_idx;")
    op.execute("DROP INDEX IF EXISTS field_provenance_entity_field_idx;")
    op.execute("DROP TABLE IF EXISTS field_provenance;")
    op.execute("UPDATE import_jobs SET status = 'provisioned' WHERE status = 'merged';")
    op.execute("ALTER TABLE import_jobs DROP CONSTRAINT IF EXISTS import_jobs_status;")
    op.execute("ALTER TABLE import_jobs DROP CONSTRAINT IF EXISTS import_jobs_status_check;")
    op.execute(
        "ALTER TABLE import_jobs ADD CONSTRAINT import_jobs_status "
        "CHECK (status IN ('parsed', 'provisioned', 'failed'));"
    )
