"""Chunk, embed, and index a document the caller is allowed to write."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Chunk, Document, DocumentACL
from app.rag.chunker import chunk_markdown, parse_front_matter
from app.rag.embedder import Embedder
from app.rag.index_meta import column_type
from app.rag.tokenize import tokenize

ALLOWED_STORAGES = frozenset({"vector", "halfvec"})


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in values) + "]"


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


async def _insert_chunks(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    text_body: str,
    embedder: Embedder,
    storage: str,
) -> int:
    if storage not in ALLOWED_STORAGES:
        raise ValueError(f"Unsupported embedding storage: {storage!r}")
    chunks = chunk_markdown(text_body)
    if not chunks:
        raise ValueError("empty_document")
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
            CAST(:embedding AS {storage}),
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
                "document_id": str(document_id),
                "chunk_index": chunk.index,
                "heading": chunk.heading,
                "content": chunk.text,
                "tokens": tokens,
                "embedding": _vec_literal(vector),
                "segmented": segmented,
            },
        )
    return len(chunks)


async def ingest_text(
    session: AsyncSession,
    *,
    embedder: Embedder,
    owner_id: uuid.UUID,
    title: str,
    text: str,
    collection: str,
    visibility: str,
    source: str,
    doc_key: str | None = None,
    grant_user_ids: list[uuid.UUID] | None = None,
    document_id: uuid.UUID | None = None,
    storage: str | None = None,
) -> Document:
    chunks = chunk_markdown(text)
    if not chunks:
        raise ValueError("empty_document")
    resolved_storage = await _resolve_storage(session, storage)
    if document_id is None:
        document = Document(
            doc_key=doc_key,
            source=source,
            title=title,
            collection=collection,
            visibility=visibility,
            status="pending",
            owner_id=owner_id,
            content_text=text,
        )
        session.add(document)
        await session.flush()
    else:
        existing = await session.get(Document, document_id)
        if existing is None:
            raise ValueError("missing_document")
        document = existing
        document.title = title
        document.collection = collection
        document.visibility = visibility
        document.content_text = text
        document.status = "pending"
        await session.execute(delete(Chunk).where(Chunk.document_id == document.id))
        await session.execute(delete(DocumentACL).where(DocumentACL.document_id == document.id))
    for grant_id in grant_user_ids or []:
        session.add(
            DocumentACL(
                document_id=document.id,
                principal_type="user",
                principal_id=str(grant_id),
                permission="read",
            )
        )
    await _insert_chunks(
        session,
        document_id=document.id,
        text_body=text,
        embedder=embedder,
        storage=resolved_storage,
    )
    document.status = "ready"
    await session.flush()
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
        await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        await _insert_chunks(
            session,
            document_id=doc.id,
            text_body=doc.content_text,
            embedder=embedder,
            storage=resolved_storage,
        )
        doc.status = "ready"
        count += 1
    await session.flush()
    return count


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
