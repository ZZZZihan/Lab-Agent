"""Chunk, embed, and index a document the caller is allowed to write."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Chunk, Document, DocumentACL
from app.rag.chunker import chunk_document, parse_front_matter
from app.rag.embedder import Embedder
from app.rag.index_meta import column_type
from app.rag.tokenize import tokenize

ALLOWED_STORAGES = frozenset({"vector", "halfvec"})


@dataclass(frozen=True)
class UpsertStats:
    created: int
    updated: int
    unchanged: int
    deleted: int

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in values) + "]"


def _sha256_hex(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _resolve_storage(session: AsyncSession, storage: str | None) -> str:
    candidate = storage
    if candidate is None:
        try:
            candidate, _ = await column_type(session)
        except Exception:
            candidate = get_settings().embedding_storage
    if candidate not in ALLOWED_STORAGES:
        raise ValueError(f"Unsupported embedding storage: {candidate!r}")
    return candidate


def _add_acl_grants(
    session: AsyncSession,
    document_id: uuid.UUID,
    *,
    grant_user_ids: Sequence[uuid.UUID] = (),
    grant_group_ids: Sequence[uuid.UUID] = (),
    grant_roles: Sequence[str] = (),
) -> None:
    seen_users: set[uuid.UUID] = set()
    for grant_id in grant_user_ids:
        if grant_id in seen_users:
            continue
        seen_users.add(grant_id)
        session.add(
            DocumentACL(
                document_id=document_id,
                principal_type="user",
                principal_id=str(grant_id),
                permission="read",
            )
        )
    seen_groups: set[uuid.UUID] = set()
    for group_id in grant_group_ids:
        if group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        session.add(
            DocumentACL(
                document_id=document_id,
                principal_type="group",
                principal_id=str(group_id),
                permission="read",
            )
        )
    seen_roles: set[str] = set()
    for role in grant_roles:
        if role in seen_roles:
            continue
        seen_roles.add(role)
        session.add(
            DocumentACL(
                document_id=document_id,
                principal_type="role",
                principal_id=role,
                permission="read",
            )
        )


async def create_document(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    title: str,
    text: str,
    source_format: str = "markdown",
    collection: str = "knowledge",
    visibility: str = "lab",
    source: str = "upload",
    doc_key: str | None = None,
    grant_user_ids: Sequence[uuid.UUID] = (),
    grant_group_ids: Sequence[uuid.UUID] = (),
    grant_roles: Sequence[str] = (),
    original_filename: str | None = None,
) -> Document:
    chunks = chunk_document(text, source_format)
    if not chunks:
        raise ValueError("empty_document")
    document = Document(
        doc_key=doc_key,
        source=source,
        source_format=source_format,
        original_filename=original_filename,
        content_sha256=_sha256_hex(text),
        error_code=None,
        attempts=0,
        title=title,
        collection=collection,
        visibility=visibility,
        status="pending",
        owner_id=owner_id,
        content_text=text,
    )
    session.add(document)
    await session.flush()
    _add_acl_grants(
        session,
        document.id,
        grant_user_ids=grant_user_ids,
        grant_group_ids=grant_group_ids,
        grant_roles=grant_roles,
    )
    await session.flush()
    return document


async def index_document(
    session: AsyncSession,
    *,
    embedder: Embedder,
    document: Document,
    storage: str | None = None,
) -> int:
    resolved_storage = await _resolve_storage(session, storage)
    chunks = chunk_document(document.content_text, document.source_format or "markdown")
    if not chunks:
        raise ValueError("empty_document")
    await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
    vectors = await embedder.embed_documents([chunk.text for chunk in chunks])
    stmt = text(
        f"""
        INSERT INTO chunks (id, document_id, chunk_index, heading, content, tokens, embedding, content_tsv)
        VALUES (
            CAST(:id AS uuid),
            CAST(:document_id AS uuid),
            :chunk_index,
            :heading,
            :content,
            :tokens,
            CAST(:embedding AS {resolved_storage}),
            to_tsvector('simple', :segmented)
        )
        """
    )
    for chunk, vector in zip(chunks, vectors, strict=True):
        tokens = tokenize(chunk.text)
        segmented = " ".join(tokens)
        await session.execute(
            stmt,
            {
                "id": str(uuid.uuid4()),
                "document_id": str(document.id),
                "chunk_index": chunk.index,
                "heading": chunk.heading,
                "content": chunk.text,
                "tokens": tokens,
                "embedding": _vec_literal(vector),
                "segmented": segmented,
            },
        )
    document.status = "ready"
    document.error_code = None
    document.updated_at = func.now()  # type: ignore[assignment]
    await session.flush()
    return len(chunks)


async def ingest_text(
    session: AsyncSession,
    *,
    embedder: Embedder,
    owner_id: uuid.UUID,
    title: str,
    text: str,
    collection: str = "knowledge",
    visibility: str = "lab",
    source: str = "upload",
    source_format: str = "markdown",
    doc_key: str | None = None,
    grant_user_ids: Sequence[uuid.UUID] | None = None,
    grant_group_ids: Sequence[uuid.UUID] | None = None,
    grant_roles: Sequence[str] | None = None,
    document_id: uuid.UUID | None = None,
    original_filename: str | None = None,
    storage: str | None = None,
) -> Document:
    resolved_storage = await _resolve_storage(session, storage)
    if document_id is None:
        document = await create_document(
            session,
            owner_id=owner_id,
            title=title,
            text=text,
            source_format=source_format,
            collection=collection,
            visibility=visibility,
            source=source,
            doc_key=doc_key,
            grant_user_ids=grant_user_ids or (),
            grant_group_ids=grant_group_ids or (),
            grant_roles=grant_roles or (),
            original_filename=original_filename,
        )
    else:
        existing = await session.get(Document, document_id)
        if existing is None:
            raise ValueError("missing_document")
        document = existing
        document.title = title
        document.collection = collection
        document.visibility = visibility
        document.source_format = source_format
        document.original_filename = original_filename
        document.content_text = text
        document.content_sha256 = _sha256_hex(text)
        document.status = "pending"
        document.error_code = None
        await session.execute(delete(DocumentACL).where(DocumentACL.document_id == document.id))
        _add_acl_grants(
            session,
            document.id,
            grant_user_ids=grant_user_ids or (),
            grant_group_ids=grant_group_ids or (),
            grant_roles=grant_roles or (),
        )
        await session.flush()

    await index_document(session, embedder=embedder, document=document, storage=resolved_storage)
    return document


async def reindex_all(
    session: AsyncSession,
    embedder: Embedder,
    *,
    storage: str | None = None,
) -> int:
    """Re-chunk and re-embed all non-deleted documents synchronously."""
    resolved_storage = await _resolve_storage(session, storage)
    docs = list(
        await session.scalars(
            select(Document)
            .where(Document.deleted_at.is_(None))
            .order_by(Document.created_at)
        )
    )
    count = 0
    for doc in docs:
        doc.status = "pending"
        await index_document(session, embedder=embedder, document=doc, storage=resolved_storage)
        count += 1
    await session.flush()
    return count


async def upsert_source(
    session: AsyncSession,
    *,
    embedder: Embedder,
    owner_id: uuid.UUID,
    source: str,
    records: Sequence[tuple[dict[str, str], str]],
    grants: dict[str, list[uuid.UUID]] | None = None,
    group_grants: dict[str, list[uuid.UUID]] | None = None,
    role_grants: dict[str, list[str]] | None = None,
    storage: str | None = None,
) -> UpsertStats:
    resolved_storage = await _resolve_storage(session, storage)
    existing_docs = list(
        await session.scalars(select(Document).where(Document.source == source))
    )
    by_key: dict[str, Document] = {}
    orphans: list[Document] = []
    for doc in existing_docs:
        if doc.doc_key and doc.doc_key not in by_key:
            by_key[doc.doc_key] = doc
        else:
            orphans.append(doc)

    seen_keys: set[str] = set()
    created = 0
    updated = 0
    unchanged = 0

    for meta, body in records:
        key = meta.get("key")
        title = meta.get("title") or key or "未命名"
        collection = meta.get("collection") or "knowledge"
        visibility = meta.get("visibility") or "lab"
        grant_ids = (grants or {}).get(key or "", [])
        grant_gids = (group_grants or {}).get(key or "", [])
        grant_rnames = (role_grants or {}).get(key or "", [])
        sha = _sha256_hex(body)

        if key:
            seen_keys.add(key)

        existing = by_key.get(key) if key else None
        if existing is None:
            new_doc = await create_document(
                session,
                owner_id=owner_id,
                title=title,
                text=body,
                source_format="markdown",
                collection=collection,
                visibility=visibility,
                source=source,
                doc_key=key,
                grant_user_ids=grant_ids,
                grant_group_ids=grant_gids,
                grant_roles=grant_rnames,
            )
            await index_document(
                session, embedder=embedder, document=new_doc, storage=resolved_storage
            )
            created += 1
            continue

        # Refresh metadata and ACL grants on the existing document
        existing.owner_id = owner_id
        existing.title = title
        existing.collection = collection
        existing.visibility = visibility
        existing.deleted_at = None
        await session.execute(delete(DocumentACL).where(DocumentACL.document_id == existing.id))
        _add_acl_grants(
            session,
            existing.id,
            grant_user_ids=grant_ids,
            grant_group_ids=grant_gids,
            grant_roles=grant_rnames,
        )

        chunk_count = int(
            (
                await session.execute(
                    text("SELECT COUNT(*) FROM chunks WHERE document_id = CAST(:did AS uuid)"),
                    {"did": str(existing.id)},
                )
            ).scalar_one()
        )
        if existing.content_sha256 == sha and existing.status == "ready" and chunk_count > 0:
            unchanged += 1
            await session.flush()
        else:
            existing.content_text = body
            existing.content_sha256 = sha
            existing.source_format = "markdown"
            existing.status = "pending"
            existing.error_code = None
            await index_document(
                session, embedder=embedder, document=existing, storage=resolved_storage
            )
            updated += 1

    deleted = 0
    for doc in orphans:
        await session.delete(doc)
        deleted += 1
    for key, doc in by_key.items():
        if key not in seen_keys:
            await session.delete(doc)
            deleted += 1

    await session.flush()
    return UpsertStats(
        created=created,
        updated=updated,
        unchanged=unchanged,
        deleted=deleted,
    )


async def replace_source(
    session: AsyncSession,
    *,
    embedder: Embedder,
    owner_id: uuid.UUID,
    source: str,
    records: list[tuple[dict[str, str], str]],
    grants: dict[str, list[uuid.UUID]] | None = None,
    storage: str | None = None,
) -> int:
    resolved_storage = await _resolve_storage(session, storage)
    existing = await session.scalars(select(Document).where(Document.source == source))
    for document in existing:
        await session.delete(document)
    await session.flush()
    count = 0
    for meta, body in records:
        key = meta.get("key")
        await ingest_text(
            session,
            embedder=embedder,
            owner_id=owner_id,
            title=meta.get("title") or key or "未命名",
            text=body,
            collection=meta.get("collection") or "knowledge",
            visibility=meta.get("visibility") or "lab",
            source=source,
            doc_key=key,
            grant_user_ids=(grants or {}).get(key or "", []),
            storage=resolved_storage,
        )
        count += 1
    return count


def load_markdown(path_text: str) -> tuple[dict[str, str], str]:
    return parse_front_matter(path_text)
