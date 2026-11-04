"""ACL-scoped hybrid retrieval. The SQL filter is the authorization boundary."""

from __future__ import annotations

import time
import uuid
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
    count_row = await session.execute(
        text(f"SELECT COUNT(*) FROM chunks c JOIN documents d ON d.id = c.document_id WHERE {where}"),
        params,
    )
    visible = int(count_row.scalar_one())
    pool = int(settings.dense_pool or 80)
    fetch_limit = visible if visible <= max(pool, 500) else pool
    vector = embedder.embed_query(query)
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
                   1 - (c.embedding <=> CAST(:qvec AS vector)) AS cosine
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE {where}
            ORDER BY c.embedding <=> CAST(:qvec AS vector)
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


def principal_from_user(user_id: uuid.UUID, role: str) -> Principal:
    return Principal(id=user_id, role=role)


# tokenize is used by callers that need the same analyzer as retrieval.
__all__ = ["SearchBundle", "principal_from_user", "search_knowledge", "tokenize"]
