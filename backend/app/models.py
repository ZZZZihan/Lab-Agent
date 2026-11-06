"""Persistence models. Audit rows are append-only at the database layer."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member', 'viewer')", name="users_role"),
        CheckConstraint(
            "email IS NOT NULL OR login_name IS NOT NULL",
            name="users_login_identity",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str | None] = mapped_column(String(320), unique=True, index=True, nullable=True)
    login_name: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, nullable=True
    )
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password_by: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('lab', 'restricted', 'private')", name="documents_visibility"
        ),
        CheckConstraint(
            "collection IN ('knowledge', 'project', 'meeting', 'paper_note')",
            name="documents_collection",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed')",
            name="documents_status",
        ),
        CheckConstraint(
            "source_format IN ('markdown', 'text', 'pdf')",
            name="documents_source_format",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    doc_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="upload")
    source_format: Mapped[str] = mapped_column(String(16), default="markdown", server_default="markdown")
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    title: Mapped[str] = mapped_column(String(300))
    collection: Mapped[str] = mapped_column(String(32), default="knowledge")
    visibility: Mapped[str] = mapped_column(String(16), default="lab")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    content_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (Index("group_members_user_idx", "user_id"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )


class DocumentACL(Base):
    __tablename__ = "document_acl"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "principal_type",
            "principal_id",
            "permission",
            name="document_acl_unique",
        ),
        CheckConstraint(
            "principal_type IN ('user', 'role', 'group')",
            name="document_acl_principal_type",
        ),
        CheckConstraint(
            "permission IN ('read')",
            name="document_acl_permission",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    principal_type: Mapped[str] = mapped_column(String(16))
    principal_id: Mapped[str] = mapped_column(String(80))
    permission: Mapped[str] = mapped_column(String(16), default="read")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="chunks_doc_index"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text)
    tokens: Mapped[list[str]] = mapped_column(ARRAY(Text))
    embedding: Mapped[list[float]] = mapped_column(Vector())
    content_tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)


class EmbeddingIndexMeta(Base):
    __tablename__ = "embedding_index_meta"
    __table_args__ = (
        CheckConstraint("id = 1", name="embedding_index_meta_id"),
        CheckConstraint("storage IN ('vector', 'halfvec')", name="embedding_index_meta_storage"),
        CheckConstraint("index_kind IN ('hnsw', 'none')", name="embedding_index_meta_index_kind"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    fingerprint: Mapped[str] = mapped_column(Text)
    embedder: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    dim: Mapped[int] = mapped_column(Integer)
    storage: Mapped[str] = mapped_column(Text)
    index_kind: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _now_utc() -> datetime:
    return datetime.now(UTC)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now_utc)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    prev_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    row_hash: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)


class RevokedToken(Base):
    __tablename__ = "revoked_tokens"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApiToken(Base):
    __tablename__ = "api_tokens"
    __table_args__ = (
        Index("api_tokens_user_idx", "user_id"),
        Index("api_tokens_hash_idx", "token_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)



class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    refused: Mapped[bool] = mapped_column(Boolean, default=False)
    citations: Mapped[list[dict[str, object]]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('parsed', 'provisioned', 'merged', 'failed')",
            name="import_jobs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    original_filename: Mapped[str] = mapped_column(String(255))
    content_sha256: Mapped[str] = mapped_column(String(64), index=True)
    parser_version: Mapped[str] = mapped_column(String(32), default="v2.1-hybrid")
    as_of_time: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="parsed")
    preview: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    stats: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rag_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provisioned_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )


class LabPerson(Base):
    __tablename__ = "lab_people"
    __table_args__ = (
        UniqueConstraint("entity_fingerprint", name="lab_people_fingerprint"),
        CheckConstraint(
            "project_role IN ('applicant', 'member')",
            name="lab_people_project_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        unique=True,
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    admin_duty: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tutor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    research_specialty: Mapped[str | None] = mapped_column(Text, nullable=True)
    degree: Mapped[str | None] = mapped_column(String(32), nullable=True)
    affiliation: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project_role: Mapped[str] = mapped_column(String(16), default="member")
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    as_of_time: Mapped[date] = mapped_column(Date)
    source_import_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_jobs.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LabAchievement(Base):
    __tablename__ = "lab_achievements"
    __table_args__ = (
        UniqueConstraint("entity_fingerprint", name="lab_achievements_fingerprint"),
        CheckConstraint(
            "kind IN ('paper', 'report', 'exhibition', 'patent', 'software', 'award', 'book', 'other')",
            name="lab_achievements_kind",
        ),
        CheckConstraint(
            "parse_quality IN ('structured', 'raw')",
            name="lab_achievements_parse_quality",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    first_author: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authors_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    venue: Mapped[str | None] = mapped_column(Text, nullable=True)
    year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    support_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)
    parse_quality: Mapped[str] = mapped_column(String(16), default="structured")
    entity_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    as_of_time: Mapped[date] = mapped_column(Date)
    source_import_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_jobs.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LabProject(Base):
    __tablename__ = "lab_projects"
    __table_args__ = (
        UniqueConstraint("entity_fingerprint", name="lab_projects_fingerprint"),
        CheckConstraint(
            "status IN ('ongoing', 'completed', 'unknown')",
            name="lab_projects_status",
        ),
        CheckConstraint(
            "status_source IN ('explicit', 'inferred')",
            name="lab_projects_status_source",
        ),
        CheckConstraint(
            "lab_role IN ('host', 'participant', 'unknown')",
            name="lab_projects_lab_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(Text)
    funder: Mapped[str | None] = mapped_column(String(255), nullable=True)
    grant_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pi_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lab_role: Mapped[str] = mapped_column(String(16), default="host")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_wan: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="unknown")
    status_source: Mapped[str] = mapped_column(String(16), default="explicit")
    relationship_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)
    entity_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    as_of_time: Mapped[date] = mapped_column(Date)
    source_import_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_jobs.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class LabFacility(Base):
    __tablename__ = "lab_facilities"
    __table_args__ = (UniqueConstraint("entity_fingerprint", name="lab_facilities_fingerprint"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    category: Mapped[str] = mapped_column(String(32), default="equipment")
    name: Mapped[str] = mapped_column(String(255))
    spec: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_wan: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    usage_scenario: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text)
    entity_fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    as_of_time: Mapped[date] = mapped_column(Date)
    source_import_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("import_jobs.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FieldProvenance(Base):
    __tablename__ = "field_provenance"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('person', 'project', 'achievement', 'facility')",
            name="field_provenance_entity_type",
        ),
        CheckConstraint(
            "source_kind IN ('import', 'manual', 'legacy')",
            name="field_provenance_source_kind",
        ),
        CheckConstraint(
            "source_kind <> 'import' OR source_job_id IS NOT NULL",
            name="field_provenance_import_job_check",
        ),
        Index("field_provenance_entity_field_idx", "entity_type", "entity_id", "field"),
        Index("field_provenance_job_idx", "source_job_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    field: Mapped[str] = mapped_column(String(64))
    value: Mapped[object] = mapped_column(JSONB, nullable=True)
    source_kind: Mapped[str] = mapped_column(String(16))
    source_job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("import_jobs.id", ondelete="CASCADE"),
        nullable=True,
    )
    as_of_time: Mapped[date] = mapped_column(Date)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now_utc, server_default=func.now()
    )
    recorded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    superseded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false"
    )

