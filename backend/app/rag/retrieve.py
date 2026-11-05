"""ACL-scoped hybrid retrieval. The SQL filter is the authorization boundary."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.acl import Principal, acl_sql
from app.config import Settings
from app.rag.bm25 import bm25_score, document_frequencies, rare_coverage
from app.rag.embedder import Embedder
from app.rag.rrf import Hit, order_for_mode
from app.rag.tokenize import content_tokens, tokenize, tsquery


@dataclass
class SearchBundle:
    hits: list[Hit]
    coverage: float
    df: dict[str, int]
    n_docs: int
    elapsed_ms: float
    mode: str


ALLOWED_STORAGES = frozenset({"vector", "halfvec"})

_PGVECTOR_VERSION: tuple[int, ...] | None = None


def _parse_pgvector_version(raw: str | None) -> tuple[int, ...]:
    if not raw:
        return (0, 0)
    parts: list[int] = []
    for part in raw.strip().split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if digits:
            parts.append(int(digits))
        else:
            break
    return tuple(parts) if parts else (0, 0)


async def get_pgvector_version(session: AsyncSession) -> tuple[int, ...]:
    """Detect and cache the installed pgvector extension version."""
    global _PGVECTOR_VERSION
    if _PGVECTOR_VERSION is not None:
        return _PGVECTOR_VERSION
    row = await session.execute(
        text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    )
    extversion = row.scalar_one_or_none()
    _PGVECTOR_VERSION = _parse_pgvector_version(
        str(extversion) if extversion is not None else None
    )
    return _PGVECTOR_VERSION


def reset_pgvector_version_cache() -> None:
    """Clear the cached pgvector version (used in tests)."""
    global _PGVECTOR_VERSION
    _PGVECTOR_VERSION = None


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in values) + "]"


async def search_knowledge(
    session: AsyncSession,
    *,
    settings: Settings,
    embedder: Embedder,
    user: Principal,
    query: str,
    mode: str | None = None,
    collection: str | None = None,
    source: str | None = None,
    limit: int | None = None,
) -> SearchBundle:
    started = time.perf_counter()
    storage = settings.embedding_storage
    if storage not in ALLOWED_STORAGES:
        raise ValueError(f"Unsupported embedding storage: {storage!r}")
    active_mode = mode or settings.retrieval_mode or "dense"
    query_tokens = content_tokens(query)
    params = {
        "is_admin": user.role == "admin",
        "uid": str(user.id),
        "uid_text": str(user.id),
        "role": user.role,
        "collection": collection or "",
        "source": source or "",
    }
    where = f"""
      d.deleted_at IS NULL
      AND d.status = 'ready'
      AND (:collection = '' OR d.collection = :collection)
      AND (
        (:source = '' AND d.source <> 'eval')
        OR d.source = :source
      )
      AND {acl_sql()}
    """
    pool = int(settings.dense_pool or 80)
    count_cap = max(pool, 500) + 1
    count_row = await session.execute(
        text(
            f"""
            SELECT COUNT(*) FROM (
                SELECT 1
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE {where}
                LIMIT :count_cap
            ) visible_sub
            """
        ),
        {**params, "count_cap": count_cap},
    )
    visible = int(count_row.scalar_one())
    if settings.resolved_embedding_index == "hnsw_prefix":
        fetch_limit = pool
    else:
        fetch_limit = visible if visible <= max(pool, 500) else pool
    vector = await embedder.embed_query(query)
    if settings.resolved_embedding_index in {"hnsw", "hnsw_prefix"}:
        ef_search = max(int(pool) * 2, 100)
        await session.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
        await session.execute(text("SET LOCAL random_page_cost = 1.0"))
        await session.execute(text("SET LOCAL plan_cache_mode = force_custom_plan"))
        if await get_pgvector_version(session) >= (0, 8):
            await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
    if settings.resolved_embedding_index == "hnsw_prefix":
        prefix_dim = int(settings.embedding_prefix_dim)
        rows = await session.execute(
            text(
                f"""
                SELECT chunk_id,
                       document_id,
                       doc_key,
                       chunk_index,
                       heading,
                       title,
                       collection,
                       content,
                       tokens,
                       1 - (embedding <=> CAST(:qvec AS {storage})) AS cosine
                FROM (
                    SELECT c.id::text AS chunk_id,
                           c.document_id::text AS document_id,
                           d.doc_key AS doc_key,
                           c.chunk_index AS chunk_index,
                           c.heading AS heading,
                           d.title AS title,
                           d.collection AS collection,
                           c.content AS content,
                           c.tokens AS tokens,
                           c.embedding AS embedding
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE {where}
                    ORDER BY subvector(c.embedding, 1, {prefix_dim})::halfvec({prefix_dim}) <=> CAST(:qvec_prefix AS halfvec({prefix_dim}))
                    LIMIT :lim
                ) sub
                ORDER BY embedding <=> CAST(:qvec AS {storage})
                """
            ),
            {
                **params,
                "qvec": _vec_literal(vector),
                "qvec_prefix": _vec_literal(vector[:prefix_dim]),
                "lim": fetch_limit,
            },
        )
    else:
        rows = await session.execute(
            text(
                f"""
                SELECT c.id::text AS chunk_id,
                       c.document_id::text AS document_id,
                       d.doc_key AS doc_key,
                       c.chunk_index AS chunk_index,
                       c.heading AS heading,
                       d.title AS title,
                       d.collection AS collection,
                       c.content AS content,
                       c.tokens AS tokens,
                       1 - (c.embedding <=> CAST(:qvec AS {storage})) AS cosine
                FROM chunks c
                JOIN documents d ON d.id = c.document_id
                WHERE {where}
                ORDER BY c.embedding <=> CAST(:qvec AS {storage})
                LIMIT :lim
                """
            ),
            {**params, "qvec": _vec_literal(vector), "lim": fetch_limit},
        )
    raw = list(rows.mappings())
    if visible > fetch_limit:
        lexical_query = tsquery(query_tokens)
        if lexical_query:
            extra = await session.execute(
                text(
                    f"""
                    SELECT c.id::text AS chunk_id,
                           c.document_id::text AS document_id,
                           d.doc_key AS doc_key,
                           c.chunk_index AS chunk_index,
                           c.heading AS heading,
                           d.title AS title,
                           d.collection AS collection,
                           c.content AS content,
                           c.tokens AS tokens,
                           NULL::float AS cosine
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    WHERE {where}
                      AND c.content_tsv @@ to_tsquery('simple', :tsq)
                    LIMIT :lim
                    """
                ),
                {**params, "tsq": lexical_query, "lim": int(settings.lexical_pool or 80)},
            )
            known = {str(item["chunk_id"]) for item in raw}
            for item in extra.mappings():
                if str(item["chunk_id"]) not in known:
                    raw.append(item)
    hits: list[Hit] = []
    token_rows: list[list[str]] = []
    for item in raw:
        tokens = list(item["tokens"] or [])
        token_rows.append(tokens)
        key = f"{item['doc_key'] or item['document_id']}#{item['chunk_index']}"
        cosine = item["cosine"]
        hits.append(
            Hit(
                chunk_id=str(item["chunk_id"]),
                document_id=str(item["document_id"]),
                doc_key=item["doc_key"],
                chunk_index=int(item["chunk_index"]),
                heading=str(item["heading"] or ""),
                title=str(item["title"]),
                collection=str(item["collection"]),
                content=str(item["content"]),
                tokens=tokens,
                cosine=float(cosine) if cosine is not None else 0.0,
                key=key,
            )
        )
    df = document_frequencies(token_rows)
    n_docs = max(len(hits), 1)
    avgdl = (sum(len(tokens) for tokens in token_rows) / n_docs) if token_rows else 1.0
    for hit in hits:
        hit["bm25"] = bm25_score(
            query_tokens,
            list(hit.get("tokens") or []),
            df,
            n_docs,
            avgdl,
            k1=settings.bm25_k1,
            b=settings.bm25_b,
        )
    ranked = order_for_mode(
        hits,
        active_mode,
        rrf_k=int(settings.rrf_k or 60),
        query_tokens=query_tokens,
        df=df,
        n_docs=n_docs,
    )
    if limit is not None:
        ranked_for_return = ranked[:limit]
    else:
        ranked_for_return = ranked
    evidence: list[str] = []
    for hit in ranked[:3]:
        evidence.extend(list(hit.get("tokens") or []))
    coverage = rare_coverage(query_tokens, evidence, df, n_docs) if query_tokens else 0.0
    elapsed = (time.perf_counter() - started) * 1000
    return SearchBundle(
        hits=ranked_for_return,
        coverage=coverage,
        df=df,
        n_docs=n_docs,
        elapsed_ms=elapsed,
        mode=active_mode,
    )


def principal_from_user(
    user_id: uuid.UUID,
    role: str,
    group_ids: Iterable[str] = (),
) -> Principal:
    return Principal(
        id=user_id,
        role=role,
        group_ids=frozenset(str(group_id) for group_id in group_ids),
    )


# tokenize is used by callers that need the same analyzer as retrieval.
__all__ = [
    "SearchBundle",
    "get_pgvector_version",
    "principal_from_user",
    "reset_pgvector_version_cache",
    "search_knowledge",
    "tokenize",
]
