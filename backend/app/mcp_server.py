"""MCP adapter. The configured actor is still subject to document ACL."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import ipaddress
import json
import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.acl import acl_sql, load_user_group_ids
from app.agent import run_agent
from app.audit import write_audit
from app.auth import PAT_PREFIX, authenticate_pat, ensure_utc
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.llm import LLM, build_llm
from app.models import User
from app.rag.embedder import Embedder, build_embedder
from app.rag.rerank import close_rerank_client, reset_rerank_client
from app.ratelimit import RateLimiter
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
_LIMITER: RateLimiter | None = None
_MCP_HTTP_ACTOR_ID: contextvars.ContextVar[uuid.UUID | None] = contextvars.ContextVar(
    "_MCP_HTTP_ACTOR_ID",
    default=None,
)


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


def get_mcp_limiter() -> RateLimiter:
    """Return the lazy module-level RateLimiter singleton for MCP tool calls."""
    global _LIMITER
    settings = get_mcp_settings()
    if _LIMITER is None:
        _LIMITER = RateLimiter(settings)
    return _LIMITER


async def close_mcp_singletons() -> None:
    """Close and clear lazy MCP singletons (embedder, llm, reranker, limiter, settings)."""
    global _SETTINGS, _EMBEDDER, _LLM, _LIMITER
    if _EMBEDDER is not None:
        await _EMBEDDER.aclose()
        _EMBEDDER = None
    if _LLM is not None:
        await _LLM.aclose()
        _LLM = None
    await close_rerank_client()
    if _LIMITER is not None:
        await _LIMITER.close()
        _LIMITER = None
    _SETTINGS = None


def reset_mcp_singletons() -> None:
    """Reset lazy MCP singletons without awaiting close (for synchronous test setup)."""
    global _SETTINGS, _EMBEDDER, _LLM, _LIMITER
    _SETTINGS = None
    _EMBEDDER = None
    _LLM = None
    _LIMITER = None
    reset_rerank_client()


def _new_request_id() -> str:
    return f"mcp-{uuid.uuid4().hex[:12]}"


async def _noop_emit(_event: dict[str, object]) -> None:
    return None


def _check_password_change_deadline(
    must_change_password_by: datetime | None,
) -> tuple[str, str] | None:
    if must_change_password_by is None:
        return None
    if datetime.now(UTC) > ensure_utc(must_change_password_by):
        return (
            "initial_password_expired",
            "初始口令已过期，请联系课题组管理员重置",
        )
    return (
        "password_change_required",
        "首次登录请先修改初始口令后再访问系统功能",
    )


async def resolve_mcp_actor(session: AsyncSession) -> tuple[User, frozenset[str]]:
    """Resolve the active MCP actor User and their group_ids.

    In Streamable HTTP mode, uses the per-request ContextVar set by `_MCPPatAuthMiddleware`.
    In stdio mode, falls back to `MCP_ACTOR_EMAIL`.
    """
    http_actor_id = _MCP_HTTP_ACTOR_ID.get()
    if http_actor_id is not None:
        user = await session.get(User, http_actor_id)
        if user is None or not user.is_active:
            raise RuntimeError("MCP actor is not provisioned")
        pw_block = _check_password_change_deadline(user.must_change_password_by)
        if pw_block is not None:
            code, _ = pw_block
            raise RuntimeError(f"MCP actor forbidden: {code}")
        group_ids = await load_user_group_ids(session, user.id)
        return user, group_ids

    email = os.environ.get("MCP_ACTOR_EMAIL", "").strip().lower()
    if not email:
        raise RuntimeError("MCP_ACTOR_EMAIL is required")
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise RuntimeError("MCP actor is not provisioned")
    pw_block = _check_password_change_deadline(user.must_change_password_by)
    if pw_block is not None:
        code, _ = pw_block
        raise RuntimeError(f"MCP actor forbidden: {code}")
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
                allow_eval=False,
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
                allow_eval=False,
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
    limiter = get_mcp_limiter()
    request_id = _new_request_id()
    async with get_sessionmaker()() as session:
        user, group_ids = await resolve_mcp_actor(session)
        actor_id = user.id
        allowed = await limiter.allow(
            f"chat:{actor_id}",
            settings.chat_rate_limit,
            settings.chat_rate_window_seconds,
        )
        if not allowed:
            await _audit_mcp_tool_call(
                settings=settings,
                actor_id=actor_id,
                tool_name="answer_question",
                status="rate_limited",
                hit_count=0,
                request_id=request_id,
            )
            await session.commit()
            return json.dumps(
                {
                    "refused": True,
                    "reason": "rate_limited",
                    "answer": "",
                    "citations": [],
                },
                ensure_ascii=False,
            )
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


def _extract_bearer_pat(scope: Scope) -> str | None:
    raw_headers = scope.get("headers")
    header_pairs = raw_headers if isinstance(raw_headers, list) else []
    for key, value in header_pairs:
        if key.lower() == b"authorization":
            text_val = value.decode("latin-1").strip()
            if text_val.lower().startswith("bearer "):
                token = text_val[7:].strip()
                if token.startswith(PAT_PREFIX):
                    return token
            return None
    return None


def _ensure_mcp_accept_header(scope: Scope) -> Scope:
    raw_headers = scope.get("headers")
    header_pairs = list(raw_headers) if isinstance(raw_headers, list) else []
    new_headers: list[tuple[bytes, bytes]] = []
    found_accept = False
    for key, value in header_pairs:
        if key.lower() == b"accept":
            found_accept = True
            current = value.decode("latin-1").lower()
            if "application/json" not in current or "text/event-stream" not in current:
                new_headers.append((b"accept", b"application/json, text/event-stream"))
            else:
                new_headers.append((key, value))
        else:
            new_headers.append((key, value))
    if not found_accept:
        new_headers.append((b"accept", b"application/json, text/event-stream"))
    return {**scope, "headers": new_headers}


def _format_host_entry(host: str) -> str:
    cleaned = host.strip().strip("[]")
    if not cleaned:
        return ""
    try:
        addr = ipaddress.ip_address(cleaned)
        if isinstance(addr, ipaddress.IPv6Address):
            return f"[{cleaned}]"
    except ValueError:
        pass
    return cleaned


def _add_host_variants(target: list[str], raw_host: str) -> None:
    formatted = _format_host_entry(raw_host)
    if not formatted:
        return
    for candidate in (formatted, f"{formatted}:*"):
        if candidate not in target:
            target.append(candidate)


def _build_transport_security(
    settings: Settings,
    *,
    host: str | None = None,
) -> TransportSecuritySettings:
    allowed_hosts: list[str] = [
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
    ]
    allowed_origins: list[str] = [
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        "http://localhost",
        "http://localhost:*",
        "http://[::1]",
        "http://[::1]:*",
        "https://127.0.0.1",
        "https://127.0.0.1:*",
        "https://localhost",
        "https://localhost:*",
        "https://[::1]",
        "https://[::1]:*",
    ]

    for url in [settings.public_base_url, *settings.cors_origin_list]:
        raw = (url or "").strip()
        if not raw:
            continue
        parsed = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
        if parsed.netloc and parsed.netloc not in allowed_hosts:
            allowed_hosts.append(parsed.netloc)
        if parsed.hostname:
            _add_host_variants(allowed_hosts, parsed.hostname)
        if parsed.scheme and parsed.netloc:
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in allowed_origins:
                allowed_origins.append(origin)

    if host and host.strip() not in {"", "0.0.0.0", "::"}:
        _add_host_variants(allowed_hosts, host.strip())

    extra_hosts = os.environ.get("MCP_ALLOWED_HOSTS", "")
    for item in extra_hosts.split(","):
        entry = item.strip()
        if not entry:
            continue
        if ":" in entry and not entry.startswith("["):
            if entry not in allowed_hosts:
                allowed_hosts.append(entry)
        else:
            _add_host_variants(allowed_hosts, entry)

    if settings.app_env == "test":
        for test_host in ("test", "testserver"):
            _add_host_variants(allowed_hosts, test_host)
            for test_origin in (f"http://{test_host}", f"http://{test_host}:*"):
                if test_origin not in allowed_origins:
                    allowed_origins.append(test_origin)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


class _MCPPatAuthMiddleware:
    """ASGI middleware that authenticates `Authorization: Bearer la_pat_...` for HTTP MCP."""

    def __init__(self, app: ASGIApp, *, session_manager: Any = None) -> None:
        self.app = app
        self.session_manager = session_manager
        self._fallback_lock = asyncio.Lock()
        self._fallback_active = 0
        self._fallback_task: asyncio.Task[None] | None = None
        self._fallback_stop: asyncio.Event | None = None

    async def _run_fallback_manager(
        self,
        ready: asyncio.Event,
        stop: asyncio.Event,
    ) -> None:
        self.session_manager._has_started = False
        async with self.session_manager.run():
            ready.set()
            await stop.wait()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_token = _extract_bearer_pat(scope)
        if not raw_token:
            resp = JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "code": "unauthorized",
                        "message": "需要提供有效的个人访问令牌 (Bearer la_pat_...)",
                    }
                },
            )
            await resp(scope, receive, send)
            return

        async with get_sessionmaker()() as session:
            try:
                actor = await authenticate_pat(session, raw_token)
            except PermissionError:
                resp = JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "unauthorized",
                            "message": "个人访问令牌无效、已过期或已吊销",
                        }
                    },
                )
                await resp(scope, receive, send)
                return

        pw_block = _check_password_change_deadline(actor.must_change_password_by)
        if pw_block is not None:
            code, message = pw_block
            resp = JSONResponse(
                status_code=403,
                content={"error": {"code": code, "message": message}},
            )
            await resp(scope, receive, send)
            return

        patched_scope = _ensure_mcp_accept_header(scope)
        ctx_token = _MCP_HTTP_ACTOR_ID.set(actor.id)
        used_fallback = False
        try:
            if self.session_manager is not None and (
                getattr(self.session_manager, "_task_group", None) is None
                or self._fallback_task is not None
            ):
                async with self._fallback_lock:
                    if (
                        self._fallback_task is None
                        and getattr(self.session_manager, "_task_group", None) is None
                    ):
                        ready = asyncio.Event()
                        stop = asyncio.Event()
                        task = asyncio.create_task(self._run_fallback_manager(ready, stop))
                        done_waiter = asyncio.create_task(ready.wait())
                        try:
                            finished, _ = await asyncio.wait(
                                {task, done_waiter},
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if task in finished and task.done():
                                await task
                        except BaseException:
                            stop.set()
                            if not task.done():
                                task.cancel()
                            with contextlib.suppress(BaseException):
                                await task
                            raise
                        finally:
                            if not done_waiter.done():
                                done_waiter.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await done_waiter
                        self._fallback_task = task
                        self._fallback_stop = stop
                    if self._fallback_task is not None:
                        self._fallback_active += 1
                        used_fallback = True
            await self.app(patched_scope, receive, send)
        finally:
            _MCP_HTTP_ACTOR_ID.reset(ctx_token)
            if used_fallback:
                async with self._fallback_lock:
                    self._fallback_active -= 1
                    if self._fallback_active == 0 and self._fallback_task is not None:
                        task_to_join = self._fallback_task
                        stop_ev = self._fallback_stop
                        self._fallback_task = None
                        self._fallback_stop = None
                        if stop_ev is not None:
                            stop_ev.set()
                        try:
                            await task_to_join
                        except BaseException:
                            if not task_to_join.done():
                                task_to_join.cancel()
                                with contextlib.suppress(BaseException):
                                    await task_to_join
                            raise


def build_http_app(
    streamable_http_path: str = "/mcp",
    *,
    host: str | None = None,
) -> Starlette:
    """Build a Streamable HTTP MCP Starlette ASGI app protected by Personal Access Tokens."""
    settings = get_mcp_settings()
    server = build_server()
    starlette_app = server.streamable_http_app(
        streamable_http_path=streamable_http_path,
        stateless_http=True,
        json_response=True,
        transport_security=_build_transport_security(settings, host=host),
    )
    orig_lifespan = starlette_app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _lifespan(app: Starlette) -> AsyncIterator[Any]:
        try:
            async with orig_lifespan(app) as state:
                yield state
        finally:
            await close_mcp_singletons()

    starlette_app.router.lifespan_context = _lifespan
    starlette_app.add_middleware(
        _MCPPatAuthMiddleware,
        session_manager=getattr(server, "_session_manager", None)
        or getattr(server._lowlevel_server, "_session_manager", None),
    )
    return starlette_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Lab Agent MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "streamable-http"],
        default="stdio",
        help="MCP transport protocol (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=8001, help="HTTP bind port")
    args = parser.parse_args(argv)

    if args.transport == "stdio":
        if not os.environ.get("MCP_ACTOR_EMAIL"):
            raise SystemExit("MCP_ACTOR_EMAIL is required")
        build_server().run(transport="stdio")
    else:
        import uvicorn

        uvicorn.run(build_http_app(host=args.host), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
