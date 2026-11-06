"""Reciprocal rank fusion and the refusal/citation rule.

Scores from different channels are never min-max normalized and then averaged.
Refusal uses the calibrated decision score (cosine, or the reranker score when
reranking is the active mode).
"""

from __future__ import annotations

from typing import TypedDict, cast


class Hit(TypedDict, total=False):
    chunk_id: str
    document_id: str
    doc_key: str | None
    chunk_index: int
    heading: str
    title: str
    collection: str
    content: str
    tokens: list[str]
    cosine: float
    bm25: float
    rrf: float
    rerank: float
    rerank_score: float
    dense_rank: int | None
    bm25_rank: int | None
    key: str


class Decision(TypedDict):
    refused: bool
    reason: str
    score: float
    cosine: float
    coverage: float
    citations: list[Hit]
    neighbors: list[Hit]


def assign_ranks(hits: list[Hit], rrf_k: int) -> None:
    dense_order = sorted(hits, key=lambda hit: (-float(hit.get("cosine") or 0.0), hit["key"]))
    for index, hit in enumerate(dense_order, start=1):
        hit["dense_rank"] = index if hit.get("cosine") is not None else None
    lexical = [hit for hit in hits if float(hit.get("bm25") or 0.0) > 0]
    lexical.sort(key=lambda hit: (-float(hit["bm25"]), hit["key"]))
    ranked_ids = set()
    for index, hit in enumerate(lexical, start=1):
        hit["bm25_rank"] = index
        ranked_ids.add(hit["key"])
    for hit in hits:
        if hit["key"] not in ranked_ids:
            hit["bm25_rank"] = None
        dense_rank = hit.get("dense_rank")
        bm25_rank = hit.get("bm25_rank")
        score = 0.0
        if isinstance(dense_rank, int):
            score += 1.0 / (rrf_k + dense_rank)
        if isinstance(bm25_rank, int):
            score += 1.0 / (rrf_k + bm25_rank)
        hit["rrf"] = score


def _num(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def order_for_mode(
    hits: list[Hit],
    mode: str,
    *,
    rrf_k: int,
    query_tokens: list[str],
    df: dict[str, int],
    n_docs: int,
) -> list[Hit]:
    from app.rag.bm25 import rare_coverage

    working = [cast(Hit, dict(hit)) for hit in hits]
    assign_ranks(working, rrf_k)
    if mode == "dense":
        working.sort(key=lambda hit: (-_num(hit.get("cosine")), str(hit["key"])))
        return working
    working.sort(key=lambda hit: (-_num(hit.get("rrf")), -_num(hit.get("cosine"))))
    if mode == "hybrid_rrf":
        return working
    if mode == "hybrid_rrf_rerank":
        reranked = [
            hit
            for hit in working
            if hit.get("rerank_score") is not None or hit.get("rerank") is not None
        ]
        if not reranked:
            return working
        unreranked = [
            hit
            for hit in working
            if hit.get("rerank_score") is None and hit.get("rerank") is None
        ]
        for hit in reranked:
            score_val = (
                _num(hit.get("rerank_score"))
                if hit.get("rerank_score") is not None
                else _num(hit.get("rerank"))
            )
            hit["rerank_score"] = score_val
            hit["rerank"] = score_val
        reranked.sort(
            key=lambda hit: (
                -_num(hit.get("rerank_score")),
                -_num(hit.get("rrf")),
                -_num(hit.get("cosine")),
                str(hit["key"]),
            )
        )
        return reranked + unreranked
    pool = 15 if mode.endswith("15") else 40
    head = working[:pool]
    tail = working[pool:]
    for hit in head:
        raw_tokens = hit.get("tokens")
        tokens = [str(token) for token in raw_tokens] if isinstance(raw_tokens, list) else []
        hit["rerank"] = rare_coverage(query_tokens, tokens, df, n_docs)
    head.sort(key=lambda hit: (-_num(hit.get("rerank")), str(hit["key"])))
    return head + tail


def decision_score(hit: Hit, mode: str) -> float:
    if mode == "hybrid_rrf_rerank":
        if hit.get("rerank_score") is not None:
            return float(hit["rerank_score"])
        return float(hit.get("rerank") or 0.0)
    if "rerank" in mode:
        return float(hit.get("rerank") or 0.0)
    return float(hit.get("cosine") or 0.0)


def decide(
    ranked: list[Hit],
    *,
    mode: str,
    score_min: float,
    rare_coverage: float,
    rare_min: float,
    margin: float,
    cite_k: int,
) -> Decision:
    neighbors = ranked[:3]
    if not ranked:
        return Decision(
            refused=True,
            reason="no_hits",
            score=0.0,
            cosine=0.0,
            coverage=rare_coverage,
            citations=[],
            neighbors=[],
        )
    top = ranked[0]
    score = decision_score(top, mode)
    cosine = float(top.get("cosine") or 0.0)
    if score < score_min or rare_coverage < rare_min:
        reason = "low_coverage" if rare_coverage < rare_min and score >= score_min else "low_score"
        return Decision(
            refused=True,
            reason=reason,
            score=score,
            cosine=cosine,
            coverage=rare_coverage,
            citations=[],
            neighbors=neighbors,
        )
    citations: list[Hit] = []
    for hit in ranked:
        value = decision_score(hit, mode)
        if value < score_min:
            continue
        if citations and value < score - margin:
            continue
        citations.append(hit)
        if len(citations) >= cite_k:
            break
    if not citations:
        return Decision(
            refused=True,
            reason="low_score",
            score=score,
            cosine=cosine,
            coverage=rare_coverage,
            citations=[],
            neighbors=neighbors,
        )
    return Decision(
        refused=False,
        reason="grounded",
        score=score,
        cosine=cosine,
        coverage=rare_coverage,
        citations=citations,
        neighbors=neighbors,
    )
