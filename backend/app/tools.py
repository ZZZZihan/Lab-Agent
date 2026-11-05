"""Agent tools. Paper search is an interface; projects and meetings read the lab corpus."""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.acl import Principal, can_read, load_user_group_ids
from app.audit import write_audit
from app.config import Settings
from app.models import Document, DocumentACL, User
from app.rag.embedder import Embedder
from app.rag.rerank import RerankClient
from app.rag.retrieve import principal_from_user, search_knowledge
from app.rag.rrf import Hit


class PaperSearch(Protocol):
    async def search(self, query: str, limit: int = 5) -> dict[str, object]: ...


class UnconfiguredPaperSearch:
    async def search(self, query: str, limit: int = 5) -> dict[str, object]:
        del query, limit
        return {
            "status": "not_configured",
            "provider": None,
            "papers": [],
        }


def build_paper_search(settings: Settings) -> PaperSearch:
    if settings.paper_search_provider == "unconfigured":
        return UnconfiguredPaperSearch()
    return UnconfiguredPaperSearch()


async def search_knowledge_base(
    session: AsyncSession,
    *,
    settings: Settings,
    embedder: Embedder,
    user: User,
    query: str,
    k: int = 8,
    collection: str | None = None,
    source: str | None = None,
    request_id: str | None = None,
    group_ids: frozenset[str] | None = None,
    rerank_client: RerankClient | None = None,
) -> list[Hit]:
    resolved_groups = (
        group_ids if group_ids is not None else await load_user_group_ids(session, user.id)
    )
    bundle = await search_knowledge(
        session,
        settings=settings,
        embedder=embedder,
        user=principal_from_user(user.id, user.role, resolved_groups),
        query=query,
        collection=collection,
        source=source,
        limit=k,
        rerank_client=rerank_client,
        request_id=request_id,
    )
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="retrieval",
        resource_type="chunk",
        request_id=request_id,
        detail={
            "mode": bundle.mode,
            "hit_ids": [hit["chunk_id"] for hit in bundle.hits],
            "scores": [round(float(hit.get("cosine") or 0.0), 4) for hit in bundle.hits],
            "k": k,
            "collection": collection or "any",
            "elapsed_ms": round(bundle.elapsed_ms, 2),
            "coverage": round(bundle.coverage, 4),
            "query_chars": len(query),
        },
    )
    for hit in bundle.hits:
        hit["_coverage"] = bundle.coverage  # type: ignore[typeddict-unknown-key]
        hit["_elapsed_ms"] = bundle.elapsed_ms  # type: ignore[typeddict-unknown-key]
        hit["_mode"] = bundle.mode  # type: ignore[typeddict-unknown-key]
        hit["_rerank_failed"] = bundle.rerank_failed  # type: ignore[typeddict-unknown-key]
    return bundle.hits


async def fetch_document(
    session: AsyncSession,
    *,
    settings: Settings,
    user: User,
    document_id: str,
    request_id: str | None = None,
    group_ids: frozenset[str] | None = None,
) -> dict[str, object] | None:
    try:
        parsed = uuid.UUID(document_id)
    except ValueError:
        return None
    document = await session.get(Document, parsed)
    if document is None or document.deleted_at is not None:
        return None
    grants = list(
        await session.scalars(select(DocumentACL).where(DocumentACL.document_id == document.id))
    )
    resolved_groups = (
        group_ids if group_ids is not None else await load_user_group_ids(session, user.id)
    )
    allowed = can_read(
        Principal(id=user.id, role=user.role, group_ids=resolved_groups),
        document,
        grants,
    )
    if not allowed:
        await write_audit(
            settings=settings,
            actor_id=user.id,
            action="permission_denied",
            resource_type="document",
            resource_id=str(document.id),
            request_id=request_id,
            detail={"tool": "fetch_document"},
        )
        return None
    return {
        "document_id": str(document.id),
        "title": document.title,
        "collection": document.collection,
        "visibility": document.visibility,
        "text": document.content_text[:6000],
    }


async def query_lab_projects(
    session: AsyncSession,
    *,
    settings: Settings,
    embedder: Embedder,
    user: User,
    query: str,
    k: int = 8,
    request_id: str | None = None,
    group_ids: frozenset[str] | None = None,
    rerank_client: RerankClient | None = None,
) -> dict[str, object]:
    hits = await search_knowledge_base(
        session,
        settings=settings,
        embedder=embedder,
        user=user,
        query=query,
        k=k,
        collection="project",
        request_id=request_id,
        group_ids=group_ids,
        rerank_client=rerank_client,
    )
    return {"status": "ok", "source": "knowledge_base", "hits": hits}


async def query_meetings(
    session: AsyncSession,
    *,
    settings: Settings,
    embedder: Embedder,
    user: User,
    query: str,
    k: int = 8,
    request_id: str | None = None,
    group_ids: frozenset[str] | None = None,
    rerank_client: RerankClient | None = None,
) -> dict[str, object]:
    hits = await search_knowledge_base(
        session,
        settings=settings,
        embedder=embedder,
        user=user,
        query=query,
        k=k,
        collection="meeting",
        request_id=request_id,
        group_ids=group_ids,
        rerank_client=rerank_client,
    )
    return {"status": "ok", "source": "knowledge_base", "hits": hits}


async def search_papers(
    *,
    settings: Settings,
    user: User,
    query: str,
    request_id: str | None = None,
) -> dict[str, object]:
    return await build_paper_search(settings).search(query)
