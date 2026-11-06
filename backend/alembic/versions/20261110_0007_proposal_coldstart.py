"""Add proposal cold-start tables (import_jobs, lab_people, lab_achievements, lab_projects, lab_facilities) and 3-tier login fields on users."""

from __future__ import annotations

from alembic import op

revision = "20261110_0007"
down_revision = "20261109_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE users SET email = lower(email) WHERE email IS NOT NULL;")
    op.execute("ALTER TABLE users ALTER COLUMN email DROP NOT NULL;")
    op.execute("ALTER TABLE users ADD COLUMN login_name varchar(64) NULL;")
    op.execute("ALTER TABLE users ADD COLUMN must_change_password_by timestamptz NULL;")
    op.execute("CREATE UNIQUE INDEX ix_users_login_name ON users(login_name);")
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT users_login_identity "
        "CHECK (email IS NOT NULL OR login_name IS NOT NULL);"
    )

    op.execute(
        """
        CREATE TABLE import_jobs (
          id uuid PRIMARY KEY,
          uploaded_by uuid NOT NULL REFERENCES users(id),
          original_filename varchar(255) NOT NULL,
          content_sha256 varchar(64) NOT NULL,
          parser_version varchar(32) NOT NULL DEFAULT 'v2.1-hybrid',
          as_of_time date NULL,
          status varchar(16) NOT NULL DEFAULT 'parsed'
            CHECK (status IN ('parsed', 'provisioned', 'failed')),
          preview jsonb NOT NULL DEFAULT '{}'::jsonb,
          stats jsonb NOT NULL DEFAULT '{}'::jsonb,
          error_code varchar(64) NULL,
          rag_document_id uuid NULL REFERENCES documents(id) ON DELETE SET NULL,
          created_at timestamptz NOT NULL DEFAULT now(),
          provisioned_at timestamptz NULL,
          provisioned_by uuid NULL REFERENCES users(id)
        );
        """
    )
    op.execute("CREATE INDEX import_jobs_sha256_idx ON import_jobs(content_sha256);")

    op.execute(
        """
        CREATE TABLE lab_people (
          id uuid PRIMARY KEY,
          user_id uuid NULL UNIQUE REFERENCES users(id) ON DELETE SET NULL,
          name varchar(64) NOT NULL,
          email varchar(320) NULL,
          title varchar(64) NULL,
          admin_duty varchar(64) NULL,
          tutor_role varchar(32) NULL,
          research_specialty text NULL,
          degree varchar(32) NULL,
          affiliation varchar(255) NULL,
          project_role varchar(16) NOT NULL DEFAULT 'member'
            CHECK (project_role IN ('applicant', 'member')),
          bio text NULL,
          entity_fingerprint varchar(64) NOT NULL UNIQUE,
          as_of_time date NOT NULL,
          source_import_id uuid NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE lab_achievements (
          id uuid PRIMARY KEY,
          kind varchar(16) NOT NULL
            CHECK (kind IN ('paper', 'report', 'exhibition', 'patent', 'software', 'award', 'book', 'other')),
          title text NOT NULL,
          first_author varchar(64) NULL,
          authors_text text NULL,
          venue text NULL,
          year smallint NULL,
          support_note text NULL,
          raw_text text NOT NULL,
          parse_quality varchar(16) NOT NULL DEFAULT 'structured'
            CHECK (parse_quality IN ('structured', 'raw')),
          entity_fingerprint varchar(64) NOT NULL UNIQUE,
          as_of_time date NOT NULL,
          source_import_id uuid NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE lab_projects (
          id uuid PRIMARY KEY,
          title text NOT NULL,
          funder varchar(255) NULL,
          grant_no varchar(64) NULL,
          pi_name varchar(64) NULL,
          lab_role varchar(16) NOT NULL DEFAULT 'host'
            CHECK (lab_role IN ('host', 'participant', 'unknown')),
          start_date date NULL,
          end_date date NULL,
          amount_wan numeric(12, 2) NULL,
          status varchar(16) NOT NULL DEFAULT 'unknown'
            CHECK (status IN ('ongoing', 'completed', 'unknown')),
          status_source varchar(16) NOT NULL DEFAULT 'explicit'
            CHECK (status_source IN ('explicit', 'inferred')),
          relationship_note text NULL,
          raw_text text NOT NULL,
          entity_fingerprint varchar(64) NOT NULL UNIQUE,
          as_of_time date NOT NULL,
          source_import_id uuid NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    op.execute(
        """
        CREATE TABLE lab_facilities (
          id uuid PRIMARY KEY,
          category varchar(32) NOT NULL DEFAULT 'equipment',
          name varchar(255) NOT NULL,
          spec text NULL,
          value_wan numeric(12, 2) NULL,
          usage_scenario text NULL,
          raw_text text NOT NULL,
          entity_fingerprint varchar(64) NOT NULL UNIQUE,
          as_of_time date NOT NULL,
          source_import_id uuid NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lab_facilities;")
    op.execute("DROP TABLE IF EXISTS lab_projects;")
    op.execute("DROP TABLE IF EXISTS lab_achievements;")
    op.execute("DROP TABLE IF EXISTS lab_people;")
    op.execute("DROP INDEX IF EXISTS import_jobs_sha256_idx;")
    op.execute("DROP TABLE IF EXISTS import_jobs;")
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_login_identity;")
    op.execute("DROP INDEX IF EXISTS ix_users_login_name;")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS must_change_password_by;")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS login_name;")
    op.execute("DELETE FROM users WHERE email IS NULL;")
    op.execute("ALTER TABLE users ALTER COLUMN email SET NOT NULL;")
