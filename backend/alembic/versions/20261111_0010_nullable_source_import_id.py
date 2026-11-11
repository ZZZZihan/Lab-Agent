"""Make lab_* source_import_id nullable with ON DELETE SET NULL and detach manual assets."""

from __future__ import annotations

from alembic import op

revision = "20261111_0010"
down_revision = "20261110_0009"
branch_labels = None
depends_on = None

_LAB_TABLES: tuple[tuple[str, str], ...] = (
    ("lab_people", "person"),
    ("lab_achievements", "achievement"),
    ("lab_projects", "project"),
    ("lab_facilities", "facility"),
)


def upgrade() -> None:
    for table_name, _ in _LAB_TABLES:
        op.execute(
            f"""
            DO $$
            DECLARE
              r RECORD;
            BEGIN
              FOR r IN
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                WHERE n.nspname = current_schema()
                  AND t.relname = '{table_name}'
                  AND c.contype = 'f'
                  AND a.attname = 'source_import_id'
              LOOP
                EXECUTE format('ALTER TABLE %I DROP CONSTRAINT %I', '{table_name}', r.conname);
              END LOOP;
            END $$;
            """
        )
        op.execute(f"ALTER TABLE {table_name} ALTER COLUMN source_import_id DROP NOT NULL;")
        op.execute(
            f"ALTER TABLE {table_name} ADD CONSTRAINT {table_name}_source_import_id_fkey "
            f"FOREIGN KEY (source_import_id) REFERENCES import_jobs(id) ON DELETE SET NULL;"
        )

    # Detach manual-only entities that were previously forced onto dummy v2.1-manual jobs
    # or onto unrelated import_jobs when created manually without source_import_id.
    for table_name, entity_type in _LAB_TABLES:
        op.execute(
            f"""
            UPDATE {table_name} AS t
            SET source_import_id = NULL
            WHERE t.source_import_id IS NOT NULL
              AND (
                EXISTS (
                  SELECT 1 FROM import_jobs j
                  WHERE j.id = t.source_import_id
                    AND j.parser_version = 'v2.1-manual'
                    AND j.original_filename = 'manual-entry.docx'
                )
                OR (
                  '{entity_type}' <> 'person'
                  AND EXISTS (
                    SELECT 1 FROM field_provenance fp
                    WHERE fp.entity_type = '{entity_type}'
                      AND fp.entity_id = t.id
                      AND fp.source_kind = 'manual'
                      AND fp.superseded = false
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM field_provenance fp
                    WHERE fp.entity_type = '{entity_type}'
                      AND fp.entity_id = t.id
                      AND fp.source_kind <> 'manual'
                  )
                )
              );
            """
        )

    # Remove unreferenced dummy manual-entry placeholder import_jobs.
    op.execute(
        """
        DELETE FROM import_jobs j
        WHERE j.parser_version = 'v2.1-manual'
          AND j.original_filename = 'manual-entry.docx'
          AND NOT EXISTS (SELECT 1 FROM lab_people x WHERE x.source_import_id = j.id)
          AND NOT EXISTS (SELECT 1 FROM lab_achievements x WHERE x.source_import_id = j.id)
          AND NOT EXISTS (SELECT 1 FROM lab_projects x WHERE x.source_import_id = j.id)
          AND NOT EXISTS (SELECT 1 FROM lab_facilities x WHERE x.source_import_id = j.id)
          AND NOT EXISTS (SELECT 1 FROM field_provenance fp WHERE fp.source_job_id = j.id);
        """
    )


def downgrade() -> None:
    # Backfill a placeholder job only if NULL source_import_id rows exist on downgrade.
    op.execute(
        """
        DO $$
        DECLARE
          fallback_job_id uuid;
          fallback_user_id uuid;
        BEGIN
          IF EXISTS (SELECT 1 FROM lab_people WHERE source_import_id IS NULL)
             OR EXISTS (SELECT 1 FROM lab_achievements WHERE source_import_id IS NULL)
             OR EXISTS (SELECT 1 FROM lab_projects WHERE source_import_id IS NULL)
             OR EXISTS (SELECT 1 FROM lab_facilities WHERE source_import_id IS NULL)
          THEN
            SELECT id INTO fallback_job_id
            FROM import_jobs
            WHERE status IN ('provisioned', 'merged')
            ORDER BY provisioned_at DESC NULLS LAST, created_at DESC
            LIMIT 1;

            IF fallback_job_id IS NULL THEN
              SELECT id INTO fallback_user_id FROM users ORDER BY created_at ASC LIMIT 1;
              IF fallback_user_id IS NOT NULL THEN
                fallback_job_id := gen_random_uuid();
                INSERT INTO import_jobs (
                  id, uploaded_by, original_filename, content_sha256, parser_version,
                  as_of_time, status, preview, stats, provisioned_at, provisioned_by
                ) VALUES (
                  fallback_job_id, fallback_user_id, 'manual-entry.docx', repeat('0', 64),
                  'v2.1-manual', CURRENT_DATE, 'provisioned',
                  '{"cover": {"project_title": "课题组资料库档案"}}'::jsonb,
                  '{}'::jsonb, now(), fallback_user_id
                );
              END IF;
            END IF;

            IF fallback_job_id IS NOT NULL THEN
              UPDATE lab_people SET source_import_id = fallback_job_id WHERE source_import_id IS NULL;
              UPDATE lab_achievements SET source_import_id = fallback_job_id WHERE source_import_id IS NULL;
              UPDATE lab_projects SET source_import_id = fallback_job_id WHERE source_import_id IS NULL;
              UPDATE lab_facilities SET source_import_id = fallback_job_id WHERE source_import_id IS NULL;
            ELSE
              DELETE FROM lab_facilities WHERE source_import_id IS NULL;
              DELETE FROM lab_projects WHERE source_import_id IS NULL;
              DELETE FROM lab_achievements WHERE source_import_id IS NULL;
              DELETE FROM lab_people WHERE source_import_id IS NULL;
            END IF;
          END IF;
        END $$;
        """
    )

    for table_name, _ in _LAB_TABLES:
        op.execute(
            f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {table_name}_source_import_id_fkey;"
        )
        op.execute(f"ALTER TABLE {table_name} ALTER COLUMN source_import_id SET NOT NULL;")
        op.execute(
            f"ALTER TABLE {table_name} ADD CONSTRAINT {table_name}_source_import_id_fkey "
            f"FOREIGN KEY (source_import_id) REFERENCES import_jobs(id) ON DELETE CASCADE;"
        )
