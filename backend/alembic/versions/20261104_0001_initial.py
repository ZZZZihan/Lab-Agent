"""Initial schema: users, ACL, chunks, append-only audit."""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261104_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('admin', 'member', 'viewer')", name="users_role"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("doc_key", sa.String(80)),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("collection", sa.String(32), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "visibility IN ('lab', 'restricted', 'private')", name="documents_visibility"
        ),
        sa.CheckConstraint(
            "collection IN ('knowledge', 'project', 'meeting', 'paper_note')",
            name="documents_collection",
        ),
        sa.CheckConstraint("status IN ('pending', 'ready', 'failed')", name="documents_status"),
    )
    op.create_index("documents_source_idx", "documents", ["source"])
    op.create_index(
        "documents_doc_key_source_uq",
        "documents",
        ["doc_key", "source"],
        unique=True,
        postgresql_where=sa.text("doc_key IS NOT NULL"),
    )
    op.create_table(
        "document_acl",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("principal_type", sa.String(16), nullable=False),
        sa.Column("principal_id", sa.String(80), nullable=False),
        sa.Column("permission", sa.String(16), nullable=False),
        sa.UniqueConstraint(
            "document_id",
            "principal_type",
            "principal_id",
            "permission",
            name="document_acl_unique",
        ),
    )
    op.create_index("document_acl_doc_idx", "document_acl", ["document_id"])
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tokens", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("embedding", Vector(512), nullable=False),
        sa.Column("content_tsv", postgresql.TSVECTOR()),
        sa.UniqueConstraint("document_id", "chunk_index", name="chunks_doc_index"),
    )
    op.create_index("chunks_document_idx", "chunks", ["document_id"])
    op.execute(
        "CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX chunks_tsv_gin ON chunks USING gin (content_tsv)")
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(64)),
        sa.Column("resource_id", sa.String(80)),
        sa.Column("request_id", sa.String(64)),
        sa.Column("ip_hash", sa.String(64)),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("audit_events_created_idx", "audit_events", ["created_at"])
    op.create_index("audit_events_action_idx", "audit_events", ["action"])
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_events_append_only() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'audit_events is append-only';
          ELSIF TG_OP = 'DELETE' THEN
            IF current_setting('yanzheng.audit_retention', true) IS DISTINCT FROM 'on' THEN
              RAISE EXCEPTION 'audit_events delete requires retention mode';
            END IF;
          END IF;
          RETURN OLD;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_events_append_only
        BEFORE UPDATE OR DELETE ON audit_events
        FOR EACH ROW EXECUTE FUNCTION audit_events_append_only();
        """
    )
    op.create_table(
        "revoked_tokens",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("conversations_user_idx", "conversations", ["user_id"])
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("refused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("citations", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("messages_conversation_idx", "messages", ["conversation_id"])


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events")
    op.execute("DROP FUNCTION IF EXISTS audit_events_append_only()")
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("revoked_tokens")
    op.drop_table("audit_events")
    op.drop_table("chunks")
    op.drop_table("document_acl")
    op.drop_table("documents")
    op.drop_table("users")
