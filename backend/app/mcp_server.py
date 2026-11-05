"""MCP adapter. The configured actor is still subject to document ACL."""

from __future__ import annotations

import json
import os
import uuid

from mcp.server.mcpserver import MCPServer
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.acl import acl_sql, load_user_group_ids
from app.agent import run_agent
from app.audit import write_audit
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.llm import LLM, build_llm
from app.models import User
from app.rag.embedder import Embedder, build_embedder
from app.tools import (
    fetch_document,
    query_lab_projects,
    query_meetings,
    search_knowledge_base,
    search_papers,
)

TOOL_NAMES = [
    "search_knowledge_base",
    "list_collections",
    "get_document_excerpt",
    "fetch_document",
    "search_papers",
    "query_lab_projects",
    "query_meetings",
    "answer_question",
]

_SETTINGS: Settings | None = None
_EMBEDDER: Embedder | None = None
_LLM: LLM | None = None


def get_mcp_settings() -> Settings:
    """Return the lazy module-level Settings singleton for MCP tool calls."""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = get_settings()
    return _SETTINGS


def get_mcp_embedder() -> Embedder:
    """Return the lazy module-level Embedder singleton for MCP tool calls."""
    global _EMBEDDER
    settings = get_mcp_settings()
    if _EMBEDDER is None:
        _EMBEDDER = build_embedder(settings)
    return _EMBEDDER


def get_mcp_llm() -> LLM:
    """Return the lazy module-level LLM singleton for MCP tool calls."""
    global _LLM
    settings = get_mcp_settings()
    if _LLM is None:
        _LLM = build_llm(settings)
    return _LLM


async def close_mcp_singletons() -> None:
    """Close and clear lazy MCP singletons (embedder, llm, settings)."""
    global _SETTINGS, _EMBEDDER, _LLM
    if _EMBEDDER is not None:
        await _EMBEDDER.aclose()
        _EMBEDDER = None
    if _LLM is not None:
        await _LLM.aclose()
        _LLM = None
    _SETTINGS = None


def reset_mcp_singletons() -> None:
    """Reset lazy MCP singletons without awaiting close (for synchronous test setup)."""
    global _SETTINGS, _EMBEDDER, _LLM
    _SETTINGS = None
    _EMBEDDER = None
    _LLM = None


def _new_request_id() -> str:
    return f"mcp-{uuid.uuid4().hex[:12]}"


async def _noop_emit(_event: dict[str, object]) -> None:
    return None


async def resolve_mcp_actor(session: AsyncSession) -> tuple[User, frozenset[str]]:
    """Resolve the active MCP actor User and their group_ids from MCP_ACTOR_EMAIL."""
    email = os.environ.get("MCP_ACTOR_EMAIL", "").strip().lower()
    if not email:
        raise RuntimeError("MCP_ACTOR_EMAIL is required")
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise RuntimeError("MCP actor is not provisioned")
    group_ids = await load_user_group_ids(session, user.id)
    return user, group_ids


async def _audit_mcp_tool_call(
    *,
    settings: Settings,
    actor_id: uuid.UUID,
    tool_name: str,
    status: str,
    hit_count: int,
    request_id: str,
) -> None:
    await write_audit(
        settings=settings,
        actor_id=actor_id,
        action="tool_call",
        resource_type="mcp_tool",
        resource_id=tool_name,
        request_id=request_id,
        detail={
            "tool": tool_name,
            "status": status,
            "hit_count": int(hit_count),
            "channel": "mcp",
        },
    )


async def mcp_search_knowledge_base(query: str, k: int = 8) -> str:
    """Hybrid search over lab documents the MCP actor can read."""
    settings = get_mcp_settings()
    embedder = get_mcp_embedder()
    request_id = _new_request_id()
    async with get_sessionmaker()() as session:
        user, group_ids = await resolve_mcp_actor(session)
        actor_id = user.id
        try:
            hits = await search_knowledge_base(
                session,
                settings=settings,
                embedder=embedder,
                user=user,
                query=query,
                k=k,
                request_id=request_id,
                group_ids=group_ids,
            )
        except Exception:
            await session.rollback()
            await _audit_mcp_tool_call(
                settings=settings,
                actor_id=actor_id,
                tool_name="search_knowledge_base",
                status="error",
                hit_count=0,
                request_id=request_id,
            )
            raise
        await _audit_mcp_tool_call(
            settings=settings,
            actor_id=actor_id,
            tool_name="search_knowledge_base",
            status="ok",
            hit_count=len(hits),
            request_id=request_id,
        )
        await session.commit()
    public = [
        {
            "chunk_id": hit["chunk_id"],
            "document_id": hit["document_id"],
            "title": hit.get("title"),
            "heading": hit.get("heading"),
            "excerpt": str(hit.get("content") or "")[:280],
            "score": round(float(hit.get("cosine") or 0.0), 4),
        }
        for hit in hits
    ]
    return json.dumps({"hits": public}, ensure_ascii=False)


# Alias mentioned in Spec T4.1
mcp_search = mcp_search_knowledge_base


async def mcp_list_collections() -> str:
    """List document collections and ACL-visible ready document counts for the MCP actor."""
    settings = get_mcp_settings()
    request_id = _new_request_id()
    async with get_sessionmaker()() as session:
        user, _group_ids = await resolve_mcp_actor(session)
        actor_id = user.id
        try:
            rows = await session.execute(
                text(
                    f"""
                    SELECT d.collection AS collection, COUNT(*) AS doc_count
                    FROM documents d
                    WHERE d.deleted_at IS NULL
                      AND d.status = 'ready'
                      AND d.source <> 'eval'
                      AND {acl_sql()}
                    GROUP BY d.collection
                    ORDER BY d.collection ASC
                    """
                ),
                {
                    "is_admin": user.role == "admin",
                    "uid": str(actor_id),
                    "uid_text": str(actor_id),
                    "role": user.role,
                },
            )
            counts_by_name = {str(r["collection"]): int(r["doc_count"]) for r in rows.mappings()}
            collections: list[dict[str, object]] = [
                {"collection": name, "document_count": counts_by_name.get(name, 0)}
                for name in ("knowledge", "project", "meeting", "paper_note")
            ]
            total_docs = sum(
                counts_by_name.get(name, 0)
                for name in ("knowledge", "project", "meeting", "paper_note")
            )
        except Exception:
            await session.rollback()
            await _audit_mcp_tool_call(
                settings=settings,
                actor_id=actor_id,
                tool_name="list_collections",
                status="error",
                hit_count=0,
                request_id=request_id,
            )
            raise
        await _audit_mcp_tool_call(
            settings=settings,
            actor_id=actor_id,
            tool_name="list_collections",
            status="ok",
            hit_count=total_docs,
            request_id=request_id,
        )
        await session.commit()
    return json.dumps(
        {
            "status": "ok",
            "collections": collections,
            "total_documents": total_docs,
        },
        ensure_ascii=False,
    )


async def mcp_get_document_excerpt(document_id: str, max_chars: int = 6000) -> str:
    """Fetch an excerpt of one document by id if the MCP actor can read it."""
    settings = get_mcp_settings()
    request_id = _new_request_id()
    limit = max(1, min(int(max_chars), 6000))
    async with get_sessionmaker()() as session:
        user, group_ids = await resolve_mcp_actor(session)
        actor_id = user.id
        try:
            document = await fetch_document(
                session,
                settings=settings,
                user=user,
                document_id=document_id,
                request_id=request_id,
                group_ids=group_ids,
            )
        except Exception:
            await session.rollback()
            await _audit_mcp_tool_call(
                settings=settings,
                actor_id=actor_id,
                tool_name="get_document_excerpt",
                status="error",
                hit_count=0,
                request_id=request_id,
            )
            raise
        status = "ok" if document is not None else "not_found"
        hit_count = 1 if document is not None else 0
        await _audit_mcp_tool_call(
            settings=settings,
            actor_id=actor_id,
            tool_name="get_document_excerpt",
            status=status,
            hit_count=hit_count,
            request_id=request_id,
        )
        await session.commit()
    if document is None:
        return json.dumps({"status": "not_found"}, ensure_ascii=False)
    excerpt = str(document.get("text") or "")[:limit]
    return json.dumps(
        {
            "status": "ok",
            "document_id": document["document_id"],
            "title": document["title"],
            "collection": document["collection"],
            "visibility": document["visibility"],
            "text": excerpt,
            "excerpt": excerpt,
        },
        ensure_ascii=False,
    )


async def mcp_fetch_document(document_id: str) -> str:
    """Fetch one document by id if the MCP actor can read it."""
    settings = get_mcp_settings()
    request_id = _new_request_id()
    async with get_sessionmaker()() as session:
        user, group_ids = await resolve_mcp_actor(session)
        actor_id = user.id
        try:
            document = await fetch_document(
                session,
                settings=settings,
                user=user,
                document_id=document_id,
                request_id=request_id,
                group_ids=group_ids,
            )
        except Exception:
            await session.rollback()
            await _audit_mcp_tool_call(
                settings=settings,
                actor_id=actor_id,
                tool_name="fetch_document",
                status="error",
                hit_count=0,
                request_id=request_id,
            )
            raise
        status = "ok" if document is not None else "not_found"
        hit_count = 1 if document is not None else 0
        await _audit_mcp_tool_call(
            settings=settings,
            actor_id=actor_id,
            tool_name="fetch_document",
            status=status,
            hit_count=hit_count,
            request_id=request_id,
        )
        await session.commit()
    return json.dumps(document or {"status": "not_found"}, ensure_ascii=False)


async def mcp_search_papers(query: str) -> str:
    """External paper search (unconfigured in MVP)."""
    settings = get_mcp_settings()
    request_id = _new_request_id()
    async with get_sessionmaker()() as session:
        user, _group_ids = await resolve_mcp_actor(session)
        actor_id = user.id
        try:
            result = await search_papers(
                settings=settings,
                user=user,
                query=query,
                request_id=request_id,
            )
        except Exception:
            await session.rollback()
            await _audit_mcp_tool_call(
                settings=settings,
                actor_id=actor_id,
                tool_name="search_papers",
                status="error",
                hit_count=0,
                request_id=request_id,
            )
            raise
        raw_papers = result.get("papers")
        hit_count = len(raw_papers) if isinstance(raw_papers, list) else 0
        status = str(result.get("status") or "not_configured")
        await _audit_mcp_tool_call(
            settings=settings,
            actor_id=actor_id,
            tool_name="search_papers",
            status=status,
            hit_count=hit_count,
            request_id=request_id,
        )
        await session.commit()
    return json.dumps(result, ensure_ascii=False)


async def mcp_query_lab_projects(query: str, k: int = 8) -> str:
    """Search lab project notes the MCP actor can read."""
    settings = get_mcp_settings()
    embedder = get_mcp_embedder()
    request_id = _new_request_id()
    async with get_sessionmaker()() as session:
        user, group_ids = await resolve_mcp_actor(session)
        actor_id = user.id
        try:
            result = await query_lab_projects(
                session,
                settings=settings,
                embedder=embedder,
                user=user,
                query=query,
                k=k,
                request_id=request_id,
                group_ids=group_ids,
            )
        except Exception:
            await session.rollback()
            await _audit_mcp_tool_call(
                settings=settings,
                actor_id=actor_id,
                tool_name="query_lab_projects",
                status="error",
                hit_count=0,
                request_id=request_id,
            )
            raise
        raw_hits = result["hits"]
        hits = raw_hits if isinstance(raw_hits, list) else []
        status = str(result.get("status") or "ok")
        await _audit_mcp_tool_call(
            settings=settings,
            actor_id=actor_id,
            tool_name="query_lab_projects",
            status=status,
            hit_count=len(hits),
            request_id=request_id,
        )
        await session.commit()
    public = [
        {
            "title": hit.get("title"),
            "heading": hit.get("heading"),
            "excerpt": str(hit.get("content") or "")[:280],
        }
        for hit in hits
    ]
    return json.dumps({"status": status, "hits": public}, ensure_ascii=False)


async def mcp_query_meetings(query: str, k: int = 8) -> str:
    """Search lab meeting notes the MCP actor can read."""
    settings = get_mcp_settings()
    embedder = get_mcp_embedder()
    request_id = _new_request_id()
    async with get_sessionmaker()() as session:
        user, group_ids = await resolve_mcp_actor(session)
        actor_id = user.id
        try:
            result = await query_meetings(
                session,
                settings=settings,
                embedder=embedder,
                user=user,
                query=query,
                k=k,
                request_id=request_id,
                group_ids=group_ids,
            )
        except Exception:
            await session.rollback()
            await _audit_mcp_tool_call(
                settings=settings,
                actor_id=actor_id,
                tool_name="query_meetings",
                status="error",
                hit_count=0,
                request_id=request_id,
            )
            raise
        raw_hits = result["hits"]
        hits = raw_hits if isinstance(raw_hits, list) else []
        status = str(result.get("status") or "ok")
        await _audit_mcp_tool_call(
            settings=settings,
            actor_id=actor_id,
            tool_name="query_meetings",
            status=status,
            hit_count=len(hits),
            request_id=request_id,
        )
        await session.commit()
    public = [
        {
            "title": hit.get("title"),
            "heading": hit.get("heading"),
            "excerpt": str(hit.get("content") or "")[:280],
        }
        for hit in hits
    ]
    return json.dumps({"status": status, "hits": public}, ensure_ascii=False)


async def mcp_answer_question(question: str) -> str:
    """Run grounded QA (run_agent) in-process as the MCP actor and return JSON."""
    settings = get_mcp_settings()
    embedder = get_mcp_embedder()
    llm = get_mcp_llm()
    request_id = _new_request_id()
    async with get_sessionmaker()() as session:
        user, group_ids = await resolve_mcp_actor(session)
        actor_id = user.id
        try:
            result = await run_agent(
                session=session,
                user=user,
                settings=settings,
                embedder=embedder,
                llm=llm,
                question=question,
                emit=_noop_emit,
                request_id=request_id,
                group_ids=group_ids,
            )
        except Exception:
            await session.rollback()
            await _audit_mcp_tool_call(
                settings=settings,
                actor_id=actor_id,
                tool_name="answer_question",
                status="error",
                hit_count=0,
                request_id=request_id,
            )
            raise
        decision = result.get("decision")
        decision_dict = decision if isinstance(decision, dict) else {}
        refused = bool(result.get("refused"))
        reason: str | None = (
            str(decision_dict.get("reason"))
            if refused and decision_dict.get("reason") is not None
            else None
        )
        has_error = False
        if result.get("embedding_error"):
            refused = True
            has_error = True
            reason = str(result.get("embedding_error"))
        elif result.get("llm_error"):
            refused = True
            has_error = True
            reason = str(result.get("llm_error"))

        answer = "" if refused else str(result.get("answer") or "")
        raw_citations = result.get("citations")
        citation_list = raw_citations if isinstance(raw_citations, list) else []
        citations = (
            []
            if refused
            else [
                {
                    "index": (
                        int(raw_idx)
                        if isinstance(raw_idx := item.get("index"), int)
                        else idx
                    ),
                    "title": str(item.get("title") or ""),
                    "heading": str(item.get("heading") or ""),
                    "document_id": str(item.get("document_id") or ""),
                    "excerpt": str(item.get("excerpt") or ""),
                }
                for idx, item in enumerate(citation_list, start=1)
                if isinstance(item, dict)
            ]
        )
        status = "error" if has_error else ("refused" if refused else "ok")
        await _audit_mcp_tool_call(
            settings=settings,
            actor_id=actor_id,
            tool_name="answer_question",
            status=status,
            hit_count=len(citations),
            request_id=request_id,
        )
        await session.commit()
    return json.dumps(
        {
            "refused": refused,
            "reason": reason,
            "answer": answer,
            "citations": citations,
        },
        ensure_ascii=False,
    )


def build_server() -> MCPServer:
    server = MCPServer(
        "lab-agent",
        instructions=(
            "Research-lab knowledge tools. Results are limited to documents the "
            "configured actor can read. search_papers stays unconfigured until a provider is set."
        ),
    )

    @server.tool(
        name="search_knowledge_base",
        description="Hybrid search over lab documents the actor can read.",
    )
    async def search_knowledge_base_tool(query: str, k: int = 8) -> str:
        return await mcp_search_knowledge_base(query=query, k=k)

    @server.tool(
        name="list_collections",
        description="List document collections and ACL-visible document counts for the actor.",
    )
    async def list_collections_tool() -> str:
        return await mcp_list_collections()

    @server.tool(
        name="get_document_excerpt",
        description="Fetch an excerpt of one document by id if the actor can read it.",
    )
    async def get_document_excerpt_tool(document_id: str, max_chars: int = 6000) -> str:
        return await mcp_get_document_excerpt(document_id=document_id, max_chars=max_chars)

    @server.tool(
        name="fetch_document",
        description="Fetch one document by id if the actor can read it.",
    )
    async def fetch_document_tool(document_id: str) -> str:
        return await mcp_fetch_document(document_id=document_id)

    @server.tool(
        name="search_papers",
        description="External paper search. Unconfigured in this MVP.",
    )
    async def search_papers_tool(query: str) -> str:
        return await mcp_search_papers(query=query)

    @server.tool(
        name="query_lab_projects",
        description="Search lab project notes the actor can read.",
    )
    async def query_lab_projects_tool(query: str, k: int = 8) -> str:
        return await mcp_query_lab_projects(query=query, k=k)

    @server.tool(
        name="query_meetings",
        description="Search lab meeting notes the actor can read.",
    )
    async def query_meetings_tool(query: str, k: int = 8) -> str:
        return await mcp_query_meetings(query=query, k=k)

    @server.tool(
        name="answer_question",
        description="Answer a question using grounded lab evidence, returning answer and citations or refusal.",
    )
    async def answer_question_tool(question: str) -> str:
        return await mcp_answer_question(question=question)

    return server


def main() -> None:
    if not os.environ.get("MCP_ACTOR_EMAIL"):
        raise SystemExit("MCP_ACTOR_EMAIL is required")
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
