"""Chunk, embed, and index a document the caller is allowed to write."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Chunk, Document, DocumentACL
from app.rag.chunker import chunk_markdown, parse_front_matter
from app.rag.embedder import Embedder
from app.rag.tokenize import tokenize


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
) -> Document:
    chunks = chunk_markdown(text)
    if not chunks:
        raise ValueError("empty_document")
    vectors = await embedder.embed_documents([chunk.text for chunk in chunks])
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
    for chunk, vector in zip(chunks, vectors, strict=True):
        row = Chunk(
            document_id=document.id,
            chunk_index=chunk.index,
            heading=chunk.heading,
            content=chunk.text,
            tokens=tokenize(chunk.text),
            embedding=vector,
        )
        session.add(row)
        await session.flush()
        segmented = " ".join(row.tokens)
        await session.execute(
            update(Chunk)
            .where(Chunk.id == row.id)
            .values(content_tsv=func.to_tsvector("simple", segmented))
        )
    document.status = "ready"
    await session.flush()
    return document


async def replace_source(
    session: AsyncSession,
    *,
    embedder: Embedder,
    owner_id: uuid.UUID,
    source: str,
    records: list[tuple[dict[str, str], str]],
    grants: dict[str, list[uuid.UUID]] | None = None,
) -> int:
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
        )
        count += 1
    return count


def load_markdown(path_text: str) -> tuple[dict[str, str], str]:
    return parse_front_matter(path_text)
