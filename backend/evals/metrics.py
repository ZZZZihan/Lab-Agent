"""Retrieval and grounding metrics. Tuning helpers refuse to see the test split."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from typing import Any

from app.rag.bm25 import rare_coverage
from app.rag.rrf import Decision, Hit, decide, order_for_mode

MODES = (
    "dense",
    "hybrid_rrf",
    "hybrid_lexical_rerank",
    "hybrid_lexical_rerank15",
    "hybrid_rrf_rerank",
)

PRIMARY_METRICS = (
    "recall_at_5",
    "mrr",
    "citation_f1",
    "refusal_correctness",
    "false_refusal_rate",
    "false_answer_rate",
)


class MetricCI(dict[str, Any]):
    """Confidence-interval dict {value, ci_low, ci_high, n} with float/comparison support."""

    def __float__(self) -> float:
        return float(self["value"])

    def __round__(self, ndigits: int | None = None) -> float:
        val = float(self["value"])
        return round(val, ndigits) if ndigits is not None else float(round(val))

    def __neg__(self) -> float:
        return -float(self["value"])

    def __gt__(self, other: object) -> bool:
        if isinstance(other, (int, float, MetricCI)):
            return float(self) > float(other)
        return NotImplemented

    def __lt__(self, other: object) -> bool:
        if isinstance(other, (int, float, MetricCI)):
            return float(self) < float(other)
        return NotImplemented


def metric_value(val: Any) -> float:
    if isinstance(val, dict):
        return float(val.get("value", 0.0))
    return float(val)


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = max(0.0, min(1.0, q)) * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def bootstrap_ci[T](
    rows: Sequence[T],
    metric_fn: Callable[[Sequence[T]], float],
    n: int = 1000,
    seed: int = 20261104,
    alpha: float = 0.05,
) -> MetricCI:
    size = len(rows)
    if size == 0:
        return MetricCI({"value": 0.0, "ci_low": 0.0, "ci_high": 0.0, "n": 0})
    value = float(metric_fn(rows))
    if n <= 0:
        return MetricCI({"value": value, "ci_low": value, "ci_high": value, "n": size})
    rng = random.Random(seed)
    samples = [
        float(metric_fn([rows[rng.randrange(size)] for _ in range(size)]))
        for _ in range(n)
    ]
    samples.sort()
    ci_low = _percentile(samples, alpha / 2.0)
    ci_high = _percentile(samples, 1.0 - alpha / 2.0)
    return MetricCI(
        {
            "value": value,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "n": size,
        }
    )


def _gold(row: dict[str, Any]) -> set[str]:
    return set(row["gold"])


def _ranked_keys(row: dict[str, Any], mode: str, rrf_k: int) -> list[Hit]:
    hits: list[Hit] = []
    for item in row["hits"]:
        hit = Hit(
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
        if mode == "hybrid_rrf_rerank":
            if item.get("rerank_score") is not None:
                hit["rerank_score"] = float(item["rerank_score"])
                hit["rerank"] = float(item["rerank_score"])
            elif item.get("rerank") is not None:
                hit["rerank"] = float(item["rerank"])
                hit["rerank_score"] = float(item["rerank"])
        hits.append(hit)
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


def _avg(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def evaluate(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    params: dict[str, Any],
    include_ci: bool = True,
) -> dict[str, Any]:
    answerable = [row for row in rows if row["answerable"]]
    unanswerable = [row for row in rows if not row["answerable"]]
    recalls: list[float] = []
    recalls_1: list[float] = []
    mrrs: list[float] = []
    citation_scores: list[float] = []
    citation_when_answered: list[float] = []
    multi_recalls: list[float] = []
    latencies: list[float] = []
    refusal_outcomes: list[float] = []
    false_refusal_outcomes: list[float] = []
    false_answer_outcomes: list[float] = []
    by_type_recalls: dict[str, list[float]] = {}
    by_type_refusals: dict[str, list[float]] = {}

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
        if mode == "hybrid_rrf_rerank" and bool(row.get("rerank_failed")):
            decision = Decision(
                refused=True,
                reason="rerank_failed",
                score=decision["score"],
                cosine=decision["cosine"],
                coverage=decision["coverage"],
                citations=[],
                neighbors=decision["neighbors"],
            )
        gold = _gold(row)
        cited = {str(hit["key"]) for hit in decision["citations"]}
        latencies.append(float(row["elapsed_ms"]))
        correct_refusal = (row["answerable"] and not decision["refused"]) or (
            not row["answerable"] and decision["refused"]
        )
        refusal_val = float(int(correct_refusal))
        refusal_outcomes.append(refusal_val)
        qtype = str(row.get("type") or "unknown")
        by_type_refusals.setdefault(qtype, []).append(refusal_val)
        if row["answerable"]:
            r5 = recall_at_k(keys, gold, 5)
            recalls.append(r5)
            by_type_recalls.setdefault(qtype, []).append(r5)
            recalls_1.append(recall_at_k(keys, gold, 1))
            mrrs.append(mrr(keys, gold))
            citation_scores.append(f1(cited, gold))
            if not decision["refused"]:
                citation_when_answered.append(f1(cited, gold))
                false_refusal_outcomes.append(0.0)
            else:
                false_refusal_outcomes.append(1.0)
            if len(gold) > 1:
                multi_recalls.append(r5)
        else:
            false_answer_outcomes.append(0.0 if decision["refused"] else 1.0)

    latencies_sorted = sorted(latencies) or [0.0]
    p50 = latencies_sorted[len(latencies_sorted) // 2]
    p95 = _percentile(latencies_sorted, 0.95)

    def _ci_or_point(vals: list[float]) -> MetricCI:
        if include_ci:
            return bootstrap_ci(vals, _avg)
        v = _avg(vals)
        return MetricCI({"value": v, "ci_low": v, "ci_high": v, "n": len(vals)})

    by_type: dict[str, dict[str, Any]] = {}
    for qtype in sorted(by_type_refusals.keys()):
        t_recalls = by_type_recalls.get(qtype, [])
        t_refusals = by_type_refusals[qtype]
        by_type[qtype] = {
            "n": len(t_refusals),
            "n_answerable": len(t_recalls),
            "recall_at_5": _avg(t_recalls) if t_recalls else None,
            "refusal_correctness": _avg(t_refusals),
        }

    return {
        "n": float(len(rows)),
        "n_answerable": float(len(answerable)),
        "n_unanswerable": float(len(unanswerable)),
        "recall_at_5": _ci_or_point(recalls),
        "recall_at_1": _avg(recalls_1),
        "mrr": _ci_or_point(mrrs),
        "multi_chunk_recall_at_5": _avg(multi_recalls),
        "citation_f1": _ci_or_point(citation_scores),
        "citation_f1_when_answered": _avg(citation_when_answered),
        "refusal_correctness": _ci_or_point(refusal_outcomes),
        "false_refusal_rate": _ci_or_point(false_refusal_outcomes),
        "false_answer_rate": _ci_or_point(false_answer_outcomes),
        "latency_ms_mean": _avg(latencies),
        "latency_ms_p50": p50,
        "latency_ms_p95": p95,
        "by_type": by_type,
    }


def tune(rows: list[dict[str, Any]], mode: str, rrf_k: int = 60) -> dict[str, Any]:
    if any(row.get("split") != "dev" for row in rows):
        raise RuntimeError("thresholds can only be tuned on the dev split")
    from app.rag.rrf import decision_score

    scores: list[float] = []
    precomputed: list[tuple[bool, list[Hit], float, set[str], float]] = []
    for row in rows:
        ranked = _ranked_keys(row, mode, rrf_k)
        if ranked:
            scores.append(decision_score(ranked[0], mode))
        keys = [str(hit["key"]) for hit in ranked]
        cov = coverage_of(row, ranked)
        gold = _gold(row)
        is_ans = bool(row["answerable"])
        r5 = recall_at_k(keys, gold, 5) if is_ans else 0.0
        precomputed.append((is_ans, ranked, cov, gold, r5))
    mean_r5 = round(_avg([r5 for is_ans, _, _, _, r5 in precomputed if is_ans]), 6)
    best_key: tuple[float, ...] | None = None
    best_params: dict[str, Any] | None = None
    for score_min in _thresholds(scores):
        for rare_min in (0.0, 0.35, 0.55, 0.75):
            for margin in (0.05, 0.12, 0.25, 1.0):
                for cite_k in (1, 2, 3, 4):
                    refusal_sum = 0.0
                    citation_sum = 0.0
                    false_refusal_sum = 0.0
                    ans_count = 0
                    for is_ans, ranked, cov, gold, _ in precomputed:
                        decision = decide(
                            ranked,
                            mode=mode,
                            score_min=float(score_min),
                            rare_coverage=cov,
                            rare_min=float(rare_min),
                            margin=float(margin),
                            cite_k=int(cite_k),
                        )
                        refused = decision["refused"]
                        if (is_ans and not refused) or (not is_ans and refused):
                            refusal_sum += 1.0
                        if is_ans:
                            ans_count += 1
                            cited = {str(hit["key"]) for hit in decision["citations"]}
                            citation_sum += f1(cited, gold)
                            if refused:
                                false_refusal_sum += 1.0
                    n_total = len(precomputed)
                    refusal_corr = refusal_sum / n_total if n_total else 0.0
                    cit_f1 = citation_sum / ans_count if ans_count else 0.0
                    false_ref = false_refusal_sum / ans_count if ans_count else 0.0
                    key = (
                        round(refusal_corr, 6),
                        round(cit_f1, 6),
                        round(-false_ref, 6),
                        mean_r5,
                    )
                    if best_key is None or key > best_key:
                        best_key = key
                        best_params = {
                            "rrf_k": rrf_k,
                            "evidence_score_min": score_min,
                            "rare_coverage_min": rare_min,
                            "cite_margin": margin,
                            "cite_k": cite_k,
                            "dense_pool": 80,
                            "lexical_pool": 80,
                            "retrieval_mode": mode,
                        }
    if best_params is None:
        raise RuntimeError("tuner received no rows")
    return {
        "params": best_params,
        "dev": evaluate(rows, mode=mode, params=best_params, include_ci=True),
    }


def _thresholds(scores: list[float]) -> list[float]:
    if not scores:
        return [0.0, 0.99]
    ordered = sorted(scores)
    picks = {0.0, round(max(ordered) + 0.001, 4)}
    for slot in range(11):
        index = min(len(ordered) - 1, int(slot / 10 * (len(ordered) - 1)))
        picks.add(round(ordered[index], 4))
    return sorted(picks)
