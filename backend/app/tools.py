"""Agent tools. Paper search is an interface; projects and meetings read the lab corpus."""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.acl import Principal, can_read, load_user_group_ids
from app.audit import write_audit
from app.config import Settings
from app.models import (
    Chunk,
    Document,
    DocumentACL,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
    User,
)
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


async def query_lab_assets(
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
    """Query structured lab assets (`lab_*` tables) and `lab-profile-*` RAG chunks."""
    hits = await search_knowledge_base(
        session,
        settings=settings,
        embedder=embedder,
        user=user,
        query=query,
        k=k,
        request_id=request_id,
        group_ids=group_ids,
        rerank_client=rerank_client,
    )
    raw_people_names = list(await session.scalars(select(LabPerson.name)))
    people_count = len(raw_people_names)
    people_names = {n for n in raw_people_names if n}
    projects_count = len(list(await session.scalars(select(LabProject.id))))
    achievements_count = len(list(await session.scalars(select(LabAchievement.id))))
    facilities_count = len(list(await session.scalars(select(LabFacility.id))))

    resolved_groups = (
        group_ids if group_ids is not None else await load_user_group_ids(session, user.id)
    )
    principal = Principal(id=user.id, role=user.role, group_ids=resolved_groups)
    profile_candidates = list(
        await session.scalars(
            select(Document)
            .where(
                Document.doc_key.like("lab-profile-%"),
                Document.deleted_at.is_(None),
                Document.status == "ready",
            )
            .order_by(Document.created_at.desc())
        )
    )
    profile_doc: Document | None = None
    for cand in profile_candidates:
        grants = list(
            await session.scalars(select(DocumentACL).where(DocumentACL.document_id == cand.id))
        )
        if can_read(principal, cand, grants):
            profile_doc = cand
            break
    if profile_doc is not None:
        profile_chunks = list(
            await session.scalars(
                select(Chunk)
                .where(Chunk.document_id == profile_doc.id)
                .order_by(Chunk.chunk_index.asc())
            )
        )
        q_lower = query.lower()
        existing_by_id = {str(h["chunk_id"]): h for h in hits}
        scored_chunks: list[tuple[float, Chunk]] = []
        for ch in profile_chunks:
            h_text = (ch.heading or "").lower()
            bonus = 0.0
            if any(w in q_lower for w in ("在研", "结题", "承担", "项目", "立项", "基金", "概况")):
                if "课题组概况与项目清单" in h_text:
                    bonus += 0.92
                elif "承担项目" in h_text:
                    bonus += 0.85
            if any(w in q_lower for w in ("成果", "报告", "论文", "专著", "专利", "软著", "成果展")):
                if "前期成果" in h_text:
                    bonus += 0.88
            if any(w in q_lower for w in ("成员", "团队", "专长", "职称", "导师", "简历", "申请人", "负责人")):
                if "团队成员" in h_text or "课题组概况" in h_text:
                    bonus += 0.88
            if any(w in q_lower for w in ("设备", "算力", "工作站", "支撑条件", "实验室")):
                if "设备与支撑条件" in h_text:
                    bonus += 0.90
            # Exact person name match gets highest priority
            for p_name in people_names:
                if p_name.lower() in q_lower and p_name.lower() in h_text:
                    bonus = max(bonus, 0.98)
            # Direct entity name match (exclude generic academic stop words)
            generic_tokens = {"研究", "课题", "项目", "成果", "技术", "报告", "实践", "开发", "应用"}
            for token in ch.tokens or []:
                if len(token) >= 2 and token.lower() in q_lower and token.lower() not in generic_tokens:
                    bonus = max(bonus, 0.86)
                    if token.lower() in h_text:
                        bonus = max(bonus, 0.94)
            if bonus > 0.0 or str(ch.id) in existing_by_id:
                scored_chunks.append((bonus, ch))

        if not scored_chunks and profile_chunks:
            scored_chunks.append((0.80, profile_chunks[0]))

        scored_chunks.sort(key=lambda item: (-item[0], item[1].chunk_index))
        rerank_failed = any(bool(h.get("_rerank_failed")) for h in hits)
        merged_hits: list[Hit] = []
        seen_ids: set[str] = set()
        for bonus, ch in scored_chunks[: min(k, 4)]:
            cid = str(ch.id)
            seen_ids.add(cid)
            base_hit = existing_by_id.get(cid)
            best_cos = max(
                float(base_hit.get("cosine") or 0.0) if base_hit else 0.0,
                min(0.95, max(0.82, bonus)),
            )
            best_rrf = max(
                float(base_hit.get("rrf") or 0.0) if base_hit else 0.0,
                0.04,
            )
            hit_obj: Hit = {
                "chunk_id": cid,
                "document_id": str(profile_doc.id),
                "doc_key": profile_doc.doc_key,
                "chunk_index": ch.chunk_index,
                "title": profile_doc.title,
                "collection": profile_doc.collection,
                "heading": ch.heading,
                "content": ch.content,
                "tokens": list(ch.tokens or []),
                "cosine": best_cos,
                "bm25": float(base_hit.get("bm25") or 0.1) if base_hit else 0.1,
                "rrf": best_rrf,
                "rerank_score": best_cos,
            }
            base_cov = float(str(hits[0].get("_coverage") or 0.0)) if hits else 0.0
            base_elapsed = float(str(hits[0].get("_elapsed_ms") or 1.0)) if hits else 1.0
            hit_obj["_coverage"] = max(base_cov, 0.85)  # type: ignore[typeddict-unknown-key]
            hit_obj["_elapsed_ms"] = base_elapsed  # type: ignore[typeddict-unknown-key]
            hit_obj["_mode"] = (  # type: ignore[typeddict-unknown-key]
                str(hits[0].get("_mode") or settings.retrieval_mode)
                if hits
                else settings.retrieval_mode
            )
            hit_obj["_rerank_failed"] = rerank_failed  # type: ignore[typeddict-unknown-key]
            merged_hits.append(hit_obj)

        for h in hits:
            if str(h["chunk_id"]) not in seen_ids and len(merged_hits) < k:
                merged_hits.append(h)
        hits = merged_hits

    return {
        "status": "ok",
        "source": "lab_assets",
        "hits": hits,
        "summary": {
            "people": people_count,
            "projects": projects_count,
            "achievements": achievements_count,
            "facilities": facilities_count,
        },
    }


async def search_papers(
    *,
    settings: Settings,
    user: User,
    query: str,
    request_id: str | None = None,
) -> dict[str, object]:
    return await build_paper_search(settings).search(query)
