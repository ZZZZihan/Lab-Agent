# ADR 0002 — Retrieval, refusal, and citations

## Status

Accepted.

## Context

An earlier lab prototype fused dense and BM25 scores with a weight tuned on an easier keyword-heavy set. On a paraphrase set that weight pulled questions toward same-topic distractors and cut Recall@5 from 0.75 to 0.42. Min-max normalizing the fused score made the top hit look like 1.0, so it could not be a refusal threshold. A cross-encoder over a 15-hit pool raised refusal quality, cost seconds per query on CPU, falsely refused about 29% of answerable questions, and could not recover facts that never entered the pool. Citation accuracy lagged recall because the supporting sentence lived in a later section while the citation pointed at the document.

## Decision

- Fusion is reciprocal rank fusion. The constant `k` is chosen on the development split by Recall@5, then MRR. There is no weighted sum of raw scores.
- The refusal score is the top hit's cosine similarity for `dense` and `hybrid_rrf`. For the lexical rerank ablations it is that rerank score. It is never a min-max normalized fusion score.
- Threshold, rare-term coverage floor, citation margin, and citation count are fit only on the development split. The test split is scored once. A missing tuned file fails closed at cosine 0.99.
- The dense candidate pool defaults to 80. The top-15 rerank mode is kept only as an ablation of the old pool limit.
- Chunks are sections under `##` headings. Gold labels and citations name `doc_key#chunk_index`, so the citation is the section that contains the fact.
- The committed baseline uses the hashing embedder and says so. Neural weights are not implied by those numbers.

## Consequences

On this synthetic set the hashing embedder ranks well and still answers too many unanswerable questions that share vocabulary, because character bigrams overlap. That is reported as a false-answer rate, not hidden by the refusal metric. Replacing the embedder with `bge-small-zh-v1.5` requires a new dev-only tune and a separately labeled baseline.
