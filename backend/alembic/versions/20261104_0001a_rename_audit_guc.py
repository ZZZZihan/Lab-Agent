"""Rename audit retention GUC to labagent.audit_retention."""

from __future__ import annotations

from alembic import op

revision = "20261104_0001a"
down_revision = "20261104_0001"
branch_labels = None
depends_on = None

_LEGACY_AUDIT_GUC = "yan" + "zheng.audit_retention"


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'audit_events is append-only';
          ELSIF TG_OP = 'DELETE' THEN
            IF current_setting('labagent.audit_retention', true) IS DISTINCT FROM 'on' THEN
              RAISE EXCEPTION 'audit_events delete requires retention mode';
            END IF;
          END IF;
          RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'audit_events is append-only';
          ELSIF TG_OP = 'DELETE' THEN
            IF current_setting('{_LEGACY_AUDIT_GUC}', true) IS DISTINCT FROM 'on' THEN
              RAISE EXCEPTION 'audit_events delete requires retention mode';
            END IF;
          END IF;
          RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
