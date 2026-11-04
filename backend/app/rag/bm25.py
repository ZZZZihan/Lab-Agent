"""Okapi BM25 over our own tokens. Fusion uses ranks, not raw scores."""

from __future__ import annotations

import math
from collections import Counter


def document_frequencies(token_rows: list[list[str]]) -> dict[str, int]:
    frequencies: dict[str, int] = {}
    for tokens in token_rows:
        for token in set(tokens):
            frequencies[token] = frequencies.get(token, 0) + 1
    return frequencies


def bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    df: dict[str, int],
    n_docs: int,
    avgdl: float,
    *,
    k1: float = 1.2,
    b: float = 0.75,
) -> float:
    if not query_tokens or not doc_tokens or avgdl <= 0:
        return 0.0
    tf = Counter(doc_tokens)
    score = 0.0
    length = len(doc_tokens)
    for token in set(query_tokens):
        freq = tf.get(token, 0)
        if freq == 0:
            continue
        df_t = df.get(token, 0)
        idf = math.log(1.0 + (n_docs - df_t + 0.5) / (df_t + 0.5))
        denom = freq + k1 * (1 - b + b * length / avgdl)
        score += idf * (freq * (k1 + 1)) / denom
    return score


def rare_coverage(
    query_tokens: list[str],
    evidence_tokens: list[str],
    df: dict[str, int],
    n_docs: int,
) -> float:
    content = list(dict.fromkeys(query_tokens))
    if not content or n_docs <= 0:
        return 0.0
    evidence = set(evidence_tokens)
    total = 0.0
    hit = 0.0
    for token in content:
        weight = math.log((n_docs + 1) / (df.get(token, 0) + 1)) + 1.0
        total += weight
        if token in evidence:
            hit += weight
    if total <= 0:
        return 0.0
    return hit / total
