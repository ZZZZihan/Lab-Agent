"""Add project groups, group_members, and document_acl CHECK constraints."""

from __future__ import annotations

from alembic import op

revision = "20261107_0004"
down_revision = "20261106_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE groups (
          id uuid PRIMARY KEY,
          slug varchar(64) UNIQUE NOT NULL,
          name varchar(120) NOT NULL,
          created_at timestamptz DEFAULT now()
        );
        """
    )
    op.execute(
        """
        CREATE TABLE group_members (
          group_id uuid REFERENCES groups(id) ON DELETE CASCADE,
          user_id uuid REFERENCES users(id) ON DELETE CASCADE,
          PRIMARY KEY (group_id, user_id)
        );
        """
    )
    op.execute("CREATE INDEX group_members_user_idx ON group_members(user_id);")
    op.execute(
        """
        ALTER TABLE document_acl ADD CONSTRAINT document_acl_principal_type
          CHECK (principal_type IN ('user','role','group'));
        """
    )
    op.execute(
        """
        ALTER TABLE document_acl ADD CONSTRAINT document_acl_permission
          CHECK (permission IN ('read'));
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE document_acl DROP CONSTRAINT IF EXISTS document_acl_permission;")
    op.execute("ALTER TABLE document_acl DROP CONSTRAINT IF EXISTS document_acl_principal_type;")
    op.execute("DROP INDEX IF EXISTS group_members_user_idx;")
    op.execute("DROP TABLE IF EXISTS group_members;")
    op.execute("DROP TABLE IF EXISTS groups;")
