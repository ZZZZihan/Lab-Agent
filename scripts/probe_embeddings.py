#!/usr/bin/env python3
"""Probe an OpenAI-compatible embedding service (Ollama by default) for Qwen3-Embedding correctness.

Contains no secrets or credentials. Talks to http://127.0.0.1:11434/v1/embeddings by default.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_MODEL = "qwen3-embedding:8b"
DEFAULT_INSTRUCTION = (
    "Given a question from a research lab member, retrieve the lab document passage that answers it"
)


def render_query(query: str, instruction: str = DEFAULT_INSTRUCTION) -> str:
    """Render a query with the official Qwen3-Embedding instruction format (no space after Query:)."""
    return f"Instruct: {instruction}\nQuery:{query}"


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return list(vec)
    return [x / norm for x in vec]


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    idx = (len(sorted_values) - 1) * p
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_values[lo]
    frac = idx - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def call_embeddings(
    base_url: str,
    model: str,
    inputs: list[str],
    *,
    dimensions: int | None = None,
    timeout: float = 120.0,
) -> tuple[list[list[float]], float]:
    url = f"{base_url.rstrip('/')}/embeddings"
    payload: dict[str, Any] = {"model": model, "input": inputs}
    if dimensions is not None:
        payload["dimensions"] = dimensions
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    items = sorted(body["data"], key=lambda item: int(item["index"]))
    vectors = [[float(x) for x in item["embedding"]] for item in items]
    return vectors, elapsed_ms


def run_probe(base_url: str, model: str, instruction: str) -> dict[str, Any]:
    # Warm up model in memory before timing
    call_embeddings(base_url, model, ["预热请求"])

    # Check dimension & MRL truncation behavior when passing dimensions=1024
    vecs_default, single_ms = call_embeddings(base_url, model, ["测试"])
    default_dim = len(vecs_default[0])
    dimensions_param_behavior: dict[str, Any]
    try:
        vecs_1024, _ = call_embeddings(base_url, model, ["测试"], dimensions=1024)
        returned_dim = len(vecs_1024[0])
        client_mrl = l2_normalize(vecs_default[0][:1024])
        cos_vs_client = (
            cosine_similarity(vecs_1024[0], client_mrl) if returned_dim == 1024 else None
        )
        dimensions_param_behavior = {
            "status": "truncated" if returned_dim == 1024 else "ignored",
            "returned_dim": returned_dim,
            "cosine_with_client_mrl_1024": round(cos_vs_client, 8) if cos_vs_client else None,
        }
    except urllib.error.HTTPError as exc:
        dimensions_param_behavior = {
            "status": "http_error",
            "http_code": exc.code,
        }

    # (a) Paraphrase vs unrelated similarity gap (>= 0.15)
    q_anchor = render_query("GPU 预约入口在哪", instruction)
    q_paraphrase = render_query("去哪里预约显卡", instruction)
    q_unrelated = render_query("周三例会在哪个会议室", instruction)
    doc_paraphrase = "去哪里预约显卡：登录清衡集群门户即可进入 GPU 预约系统。"
    doc_unrelated = "周三例会固定在三楼 302 会议室举行。"

    vecs_a, _ = call_embeddings(
        base_url,
        model,
        [q_anchor, q_paraphrase, q_unrelated, doc_paraphrase, doc_unrelated],
    )
    cos_qq_paraphrase = cosine_similarity(vecs_a[0], vecs_a[1])
    cos_qq_unrelated = cosine_similarity(vecs_a[0], vecs_a[2])
    gap_qq = cos_qq_paraphrase - cos_qq_unrelated

    cos_qd_paraphrase = cosine_similarity(vecs_a[0], vecs_a[3])
    cos_qd_unrelated = cosine_similarity(vecs_a[0], vecs_a[4])
    gap_qd = cos_qd_paraphrase - cos_qd_unrelated

    check_a_passed = gap_qq >= 0.15 and gap_qd >= 0.15

    # (b) Determinism: embed the same text twice in separate calls, cosine >= 0.9999
    probe_text = render_query("清衡-A100 集群一共有多少台 GPU 节点？", instruction)
    vec_b1, _ = call_embeddings(base_url, model, [probe_text])
    vec_b2, _ = call_embeddings(base_url, model, [probe_text])
    cos_b = cosine_similarity(vec_b1[0], vec_b2[0])
    check_b_passed = cos_b >= 0.9999

    # (c) Long input (~20000 chars): check error vs silent truncation, compare 6000 vs 6000 + 14000
    prefix_unit = "清衡实验室 GPU 集群管理与安全规范：节点使用需提前登记，数据须遵守访问控制策略。"
    prefix_6000 = (prefix_unit * ((6000 // len(prefix_unit)) + 1))[:6000]
    tail_unit = "食堂本周菜单更新：周三提供红烧肉、清炒时蔬与紫菜蛋花汤，凭校园卡在二楼窗口刷卡就餐。"
    tail_14000 = (tail_unit * ((14000 // len(tail_unit)) + 1))[:14000]
    long_20000 = prefix_6000 + tail_14000

    long_check: dict[str, Any] = {
        "prefix_chars": len(prefix_6000),
        "long_chars": len(long_20000),
    }
    try:
        vec_6k, ms_6k = call_embeddings(base_url, model, [prefix_6000])
        vec_20k, ms_20k = call_embeddings(base_url, model, [long_20000])
        cos_6k_vs_20k = cosine_similarity(vec_6k[0], vec_20k[0])
        long_check.update(
            {
                "status": "ok",
                "behavior": (
                    "truncated_at_or_before_6000_chars"
                    if cos_6k_vs_20k >= 0.9999
                    else "attends_beyond_6000_chars_without_error"
                ),
                "cosine_6000_vs_6000_plus_14000": round(cos_6k_vs_20k, 6),
                "latency_6000_ms": round(ms_6k, 2),
                "latency_20000_ms": round(ms_20k, 2),
            }
        )
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:300]
        long_check.update(
            {
                "status": "http_error",
                "http_code": exc.code,
                "error_excerpt": err_body,
            }
        )
    except Exception as exc:
        long_check.update({"status": "error", "error": str(exc)})

    # (d) Latency: 32-item batch request + 32 sequential single-item requests (p50 / p95)
    batch_texts = [
        f"## 实验室知识库段落 {i}\n清衡-A100 集群第 {i} 号实验记录与存储配额说明，包含项目归档与审计要求。"
        for i in range(32)
    ]
    _, batch_32_total_ms = call_embeddings(base_url, model, batch_texts)
    batch_32_per_item_ms = batch_32_total_ms / 32.0

    single_latencies_ms: list[float] = []
    for text in batch_texts:
        _, elapsed = call_embeddings(base_url, model, [text])
        single_latencies_ms.append(elapsed)
    single_sorted = sorted(single_latencies_ms)

    return {
        "base_url": base_url,
        "model": model,
        "default_dim": default_dim,
        "dimensions_1024_probe": dimensions_param_behavior,
        "check_a_similarity_gap": {
            "passed": check_a_passed,
            "query_to_query": {
                "cosine_paraphrase": round(cos_qq_paraphrase, 6),
                "cosine_unrelated": round(cos_qq_unrelated, 6),
                "gap": round(gap_qq, 6),
            },
            "query_to_document": {
                "cosine_paraphrase": round(cos_qd_paraphrase, 6),
                "cosine_unrelated": round(cos_qd_unrelated, 6),
                "gap": round(gap_qd, 6),
            },
        },
        "check_b_determinism": {
            "passed": check_b_passed,
            "cosine_identical_twice": round(cos_b, 8),
        },
        "check_c_long_input_20000_chars": long_check,
        "check_d_latency_ms": {
            "single_probe_ms": round(single_ms, 2),
            "batch_32_total_ms": round(batch_32_total_ms, 2),
            "batch_32_per_item_mean_ms": round(batch_32_per_item_ms, 2),
            "sequential_32_single_requests_p50_ms": round(percentile(single_sorted, 0.50), 2),
            "sequential_32_single_requests_p95_ms": round(percentile(single_sorted, 0.95), 2),
            "sequential_32_single_requests_mean_ms": round(
                statistics.fmean(single_latencies_ms), 2
            ),
        },
        "overall_passed": check_a_passed and check_b_passed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--instruction", default=DEFAULT_INSTRUCTION)
    args = parser.parse_args()

    result = run_probe(args.base_url, args.model, args.instruction)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["overall_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
