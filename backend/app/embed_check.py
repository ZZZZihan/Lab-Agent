"""Embedding service connectivity and correctness self-check (`python -m app.embed_check`)."""

from __future__ import annotations

import asyncio
import math
import sys
import time

from app.config import Settings, get_settings
from app.rag.embedder import Embedder, EmbeddingError, build_embedder

# Fixed probe sentences (do not read evaluation datasets).
ANCHOR_QUERY = "GPU 预约入口在哪"
PARAPHRASE_QUERY = "去哪里预约显卡"
UNRELATED_QUERY = "周三例会在哪个会议室"
PARAPHRASE_DOC = "去哪里预约显卡：登录清衡集群门户即可进入 GPU 预约系统。"
UNRELATED_DOC = "周三例会固定在三楼 302 会议室举行。"
DETERMINISM_QUERY = "清衡-A100 集群一共有多少台 GPU 节点？"


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


async def run_embed_check(
    settings: Settings | None = None,
    embedder: Embedder | None = None,
) -> tuple[bool, str]:
    active_settings = settings or get_settings()
    owns_embedder = embedder is None
    active_embedder = embedder or build_embedder(active_settings)

    try:
        t0 = time.perf_counter()
        q_anchor = await active_embedder.embed_query(ANCHOR_QUERY)
        q_paraphrase = await active_embedder.embed_query(PARAPHRASE_QUERY)
        q_unrelated = await active_embedder.embed_query(UNRELATED_QUERY)
        docs = await active_embedder.embed_documents([PARAPHRASE_DOC, UNRELATED_DOC])
        det_1 = await active_embedder.embed_query(DETERMINISM_QUERY)
        det_2 = await active_embedder.embed_query(DETERMINISM_QUERY)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
    except EmbeddingError as exc:
        base = active_settings.embedding_base_host or "local"
        return False, f"fail code={exc.code} base={base} fingerprint={active_settings.embedding_fingerprint}"
    except Exception as exc:
        return False, f"fail code={type(exc).__name__} fingerprint={active_settings.embedding_fingerprint}"
    finally:
        if owns_embedder:
            await active_embedder.aclose()

    dim = len(q_anchor)
    if dim != active_settings.embedding_dim:
        return (
            False,
            f"fail code=dimension_mismatch dim={dim} expected={active_settings.embedding_dim} "
            f"fingerprint={active_settings.embedding_fingerprint}",
        )

    gap_qq = _cosine(q_anchor, q_paraphrase) - _cosine(q_anchor, q_unrelated)
    gap_qd = _cosine(q_anchor, docs[0]) - _cosine(q_anchor, docs[1])
    gap = min(gap_qq, gap_qd)
    det_cos = _cosine(det_1, det_2)
    ms_per_text = elapsed_ms / 7.0

    if gap < 0.15 or det_cos < 0.9999:
        return (
            False,
            f"fail fingerprint={active_settings.embedding_fingerprint} dim={dim} "
            f"gap={gap:.4f} determinism={det_cos:.6f} ms_per_text={ms_per_text:.2f}",
        )

    return (
        True,
        f"ok fingerprint={active_settings.embedding_fingerprint} dim={dim} "
        f"gap={gap:.4f} ms_per_text={ms_per_text:.2f}",
    )


def main() -> int:
    ok, line = asyncio.run(run_embed_check())
    print(line)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
