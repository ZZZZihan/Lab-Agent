"""Retrieval and grounding metrics. Tuning helpers refuse to see the test split."""

from __future__ import annotations

from typing import Any

from app.rag.bm25 import rare_coverage
from app.rag.rrf import Hit, decide, order_for_mode


def _gold(row: dict[str, Any]) -> set[str]:
    return set(row["gold"])


def _ranked_keys(row: dict[str, Any], mode: str, rrf_k: int) -> list[Hit]:
    hits: list[Hit] = []
    for item in row["hits"]:
        hits.append(
            Hit(
                key=str(item["key"]),
                cosine=float(item["cosine"]),
                bm25=float(item["bm25"]),
                tokens=list(item.get("tokens") or []),
                content=str(item.get("content") or ""),
                chunk_id=str(item.get("chunk_id") or item["key"]),
                document_id=str(item.get("document_id") or ""),
                heading=str(item.get("heading") or ""),
                title=str(item.get("title") or ""),
                chunk_index=int(item.get("chunk_index") or 0),
            )
        )
    return order_for_mode(
        hits,
        mode,
        rrf_k=rrf_k,
        query_tokens=list(row["query_tokens"]),
        df=row["df"],
        n_docs=int(row["n_docs"]),
    )


def coverage_of(row: dict[str, Any], ranked: list[Hit]) -> float:
    evidence: list[str] = []
    for hit in ranked[:3]:
        evidence.extend(list(hit.get("tokens") or []))
    return rare_coverage(list(row["query_tokens"]), evidence, row["df"], int(row["n_docs"]))


def recall_at_k(ranked: list[str], gold: set[str], k: int) -> float:
    if not gold:
        return 0.0
    return len(set(ranked[:k]) & gold) / len(gold)


def mrr(ranked: list[str], gold: set[str]) -> float:
    for index, key in enumerate(ranked, start=1):
        if key in gold:
            return 1.0 / index
    return 0.0


def f1(pred: set[str], gold: set[str]) -> float:
    if not pred and not gold:
        return 1.0
    if not pred or not gold:
        return 0.0
    overlap = len(pred & gold)
    precision = overlap / len(pred)
    recall = overlap / len(gold)
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def evaluate(rows: list[dict[str, Any]], *, mode: str, params: dict[str, Any]) -> dict[str, float]:
    answerable = [row for row in rows if row["answerable"]]
    unanswerable = [row for row in rows if not row["answerable"]]
    recalls: list[float] = []
    recalls_1: list[float] = []
    mrrs: list[float] = []
    citation_scores: list[float] = []
    citation_when_answered: list[float] = []
    multi_recalls: list[float] = []
    latencies: list[float] = []
    refusal_hits = 0
    false_refusals = 0
    false_answers = 0
    for row in rows:
        ranked = _ranked_keys(row, mode, int(params["rrf_k"]))
        keys = [str(hit["key"]) for hit in ranked]
        coverage = coverage_of(row, ranked)
        decision = decide(
            ranked,
            mode=mode,
            score_min=float(params["evidence_score_min"]),
            rare_coverage=coverage,
            rare_min=float(params["rare_coverage_min"]),
            margin=float(params["cite_margin"]),
            cite_k=int(params["cite_k"]),
        )
        gold = _gold(row)
        cited = {str(hit["key"]) for hit in decision["citations"]}
        latencies.append(float(row["elapsed_ms"]))
        correct_refusal = (row["answerable"] and not decision["refused"]) or (
            not row["answerable"] and decision["refused"]
        )
        refusal_hits += int(correct_refusal)
        if row["answerable"]:
            recalls.append(recall_at_k(keys, gold, 5))
            recalls_1.append(recall_at_k(keys, gold, 1))
            mrrs.append(mrr(keys, gold))
            citation_scores.append(f1(cited, gold))
            if not decision["refused"]:
                citation_when_answered.append(f1(cited, gold))
            else:
                false_refusals += 1
            if len(gold) > 1:
                multi_recalls.append(recall_at_k(keys, gold, 5))
        elif not decision["refused"]:
            false_answers += 1
    latencies_sorted = sorted(latencies) or [0.0]
    p50 = latencies_sorted[len(latencies_sorted) // 2]

    def avg(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    return {
        "n": float(len(rows)),
        "n_answerable": float(len(answerable)),
        "n_unanswerable": float(len(unanswerable)),
        "recall_at_5": avg(recalls),
        "recall_at_1": avg(recalls_1),
        "mrr": avg(mrrs),
        "multi_chunk_recall_at_5": avg(multi_recalls),
        "citation_f1": avg(citation_scores),
        "citation_f1_when_answered": avg(citation_when_answered),
        "refusal_correctness": refusal_hits / len(rows) if rows else 0.0,
        "false_refusal_rate": false_refusals / len(answerable) if answerable else 0.0,
        "false_answer_rate": false_answers / len(unanswerable) if unanswerable else 0.0,
        "latency_ms_mean": avg(latencies),
        "latency_ms_p50": p50,
    }


def tune(rows: list[dict[str, Any]], mode: str, rrf_k: int = 60) -> dict[str, Any]:
    if any(row.get("split") != "dev" for row in rows):
        raise RuntimeError("thresholds can only be tuned on the dev split")
    from app.rag.rrf import decision_score

    scores: list[float] = []
    for row in rows:
        ranked = _ranked_keys(row, mode, rrf_k)
        if ranked:
            scores.append(decision_score(ranked[0], mode))
    best_key: tuple[float, ...] | None = None
    best: dict[str, Any] | None = None
    for score_min in _thresholds(scores):
        for rare_min in (0.0, 0.35, 0.55, 0.75):
            for margin in (0.05, 0.12, 0.25, 1.0):
                for cite_k in (1, 2, 3, 4):
                    params = {
                        "rrf_k": rrf_k,
                        "evidence_score_min": score_min,
                        "rare_coverage_min": rare_min,
                        "cite_margin": margin,
                        "cite_k": cite_k,
                        "dense_pool": 80,
                        "lexical_pool": 80,
                        "retrieval_mode": mode,
                    }
                    metrics = evaluate(rows, mode=mode, params=params)
                    key = (
                        round(metrics["refusal_correctness"], 6),
                        round(metrics["citation_f1"], 6),
                        round(-metrics["false_refusal_rate"], 6),
                        round(metrics["recall_at_5"], 6),
                    )
                    if best_key is None or key > best_key:
                        best_key = key
                        best = {"params": params, "dev": metrics}
    if best is None:
        raise RuntimeError("tuner received no rows")
    return best


def _thresholds(scores: list[float]) -> list[float]:
    if not scores:
        return [0.0, 0.99]
    ordered = sorted(scores)
    picks = {0.0, round(max(ordered) + 0.001, 4)}
    for slot in range(11):
        index = min(len(ordered) - 1, int(slot / 10 * (len(ordered) - 1)))
        picks.add(round(ordered[index], 4))
    return sorted(picks)
