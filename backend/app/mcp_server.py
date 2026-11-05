"""MCP adapter. The configured actor is still subject to document ACL."""

from __future__ import annotations

import json
import os

from mcp.server.mcpserver import MCPServer
from sqlalchemy import select

from app.config import get_settings
from app.db import get_sessionmaker
from app.models import User
from app.rag.embedder import build_embedder
from app.tools import (
    fetch_document,
    query_lab_projects,
    query_meetings,
    search_knowledge_base,
    search_papers,
)

TOOL_NAMES = [
    "search_knowledge_base",
    "fetch_document",
    "search_papers",
    "query_lab_projects",
    "query_meetings",
]


def build_server() -> MCPServer:
    server = MCPServer(
        "lab-agent",
        instructions=(
            "Research-lab knowledge tools. Results are limited to documents the "
            "configured actor can read. search_papers stays unconfigured until a provider is set."
        ),
    )

    async def _user(session: object) -> User:
        email = os.environ.get("MCP_ACTOR_EMAIL", "").strip().lower()
        if not email:
            raise RuntimeError("MCP_ACTOR_EMAIL is required")
        from sqlalchemy.ext.asyncio import AsyncSession

        assert isinstance(session, AsyncSession)
        user = await session.scalar(select(User).where(User.email == email))
        if user is None or not user.is_active:
            raise RuntimeError("MCP actor is not provisioned")
        return user

    @server.tool(
        name="search_knowledge_base",
        description="Hybrid search over lab documents the actor can read.",
    )
    async def search_knowledge_base_tool(query: str, k: int = 8) -> str:
        settings = get_settings()
        embedder = build_embedder(settings)
        try:
            async with get_sessionmaker()() as session:
                user = await _user(session)
                hits = await search_knowledge_base(
                    session,
                    settings=settings,
                    embedder=embedder,
                    user=user,
                    query=query,
                    k=k,
                )
                await session.commit()
        finally:
            await embedder.aclose()
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

    @server.tool(name="fetch_document", description="Fetch one document by id if the actor can read it.")
    async def fetch_document_tool(document_id: str) -> str:
        settings = get_settings()
        async with get_sessionmaker()() as session:
            user = await _user(session)
            document = await fetch_document(
                session, settings=settings, user=user, document_id=document_id
            )
            await session.commit()
        return json.dumps(document or {"status": "not_found"}, ensure_ascii=False)

    @server.tool(name="search_papers", description="External paper search. Unconfigured in this MVP.")
    async def search_papers_tool(query: str) -> str:
        settings = get_settings()
        async with get_sessionmaker()() as session:
            user = await _user(session)
            result = await search_papers(settings=settings, user=user, query=query)
            await session.commit()
        return json.dumps(result, ensure_ascii=False)

    @server.tool(name="query_lab_projects", description="Search lab project notes the actor can read.")
    async def query_lab_projects_tool(query: str, k: int = 8) -> str:
        settings = get_settings()
        embedder = build_embedder(settings)
        try:
            async with get_sessionmaker()() as session:
                user = await _user(session)
                result = await query_lab_projects(
                    session,
                    settings=settings,
                    embedder=embedder,
                    user=user,
                    query=query,
                    k=k,
                )
                await session.commit()
        finally:
            await embedder.aclose()
        raw_hits = result["hits"]
        hits = raw_hits if isinstance(raw_hits, list) else []
        public = [
            {"title": hit.get("title"), "heading": hit.get("heading"), "excerpt": str(hit.get("content") or "")[:280]}
            for hit in hits
        ]
        return json.dumps({"status": result["status"], "hits": public}, ensure_ascii=False)

    @server.tool(name="query_meetings", description="Search lab meeting notes the actor can read.")
    async def query_meetings_tool(query: str, k: int = 8) -> str:
        settings = get_settings()
        embedder = build_embedder(settings)
        try:
            async with get_sessionmaker()() as session:
                user = await _user(session)
                result = await query_meetings(
                    session,
                    settings=settings,
                    embedder=embedder,
                    user=user,
                    query=query,
                    k=k,
                )
                await session.commit()
        finally:
            await embedder.aclose()
        raw_hits = result["hits"]
        hits = raw_hits if isinstance(raw_hits, list) else []
        public = [
            {"title": hit.get("title"), "heading": hit.get("heading"), "excerpt": str(hit.get("content") or "")[:280]}
            for hit in hits
        ]
        return json.dumps({"status": result["status"], "hits": public}, ensure_ascii=False)

    return server


def main() -> None:
    if not os.environ.get("MCP_ACTOR_EMAIL"):
        raise SystemExit("MCP_ACTOR_EMAIL is required")
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
