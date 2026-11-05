"""Scale benchmark for retrieval at 20k+ chunks (Spec T3.7 / PR-27).

Compares three retrieval paths on the v1 evaluation dataset padded with synthetic
distractor chunks up to ``--chunks`` (default 20,000):
  1. ``exact_4096h``: 4096-dim ``halfvec`` exact scan (``EMBEDDING_INDEX=none``)
  2. ``two_stage_prefix1024``: 4096-dim ``halfvec`` two-stage retrieval using a
     1024-dim prefix HNSW index (``EMBEDDING_INDEX=hnsw_prefix``, ``EMBEDDING_PREFIX_DIM=1024``)
  3. ``truncated_1024_hnsw``: MRL truncated 1024-dim ``vector`` HNSW (``EMBEDDING_INDEX=hnsw``)

Also sweeps chunk counts (e.g. 1k, 5k, 10k, 20k) under exact scan to record the
empirical chunk count threshold where exact scan satisfies ``p95 <= 300 ms``.
"""

from __future__ import annotations

import argparse
import array
import ast
import asyncio
import json
import math
import random
import struct
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.acl import Principal
from app.auth import hash_password
from app.config import Settings, get_settings, repo_root, reset_settings_cache
from app.db import dispose_engine, get_sessionmaker
from app.ingest_worker import process_next_pending
from app.models import Document, User
from app.rag.chunker import chunk_markdown, parse_front_matter
from app.rag.embedder import Embedder, HashingEmbedder, build_embedder
from app.rag.index_meta import check_index
from app.rag.retrieve import reset_pgvector_version_cache, search_knowledge
from app.rag.tokenize import content_tokens, tokenize
from app.reindex import apply_reindex
from sqlalchemy import select, text

from evals.metrics import _percentile, evaluate, metric_value

DISTRACTOR_SEED_TEXTS: tuple[str, ...] = tuple(
    [
        f"Synthetic distractor passage {i}: "
        f"The study of {topic} examines structural dynamics, equilibrium states, "
        f"spectral decompositions, and empirical observations across historical cohorts "
        f"under controlled macroscopic boundary conditions (series {i * 17 + 3})."
        for i, topic in enumerate(
            [
                "paleozoic stratigraphy and marine trilobite fossil assemblages",
                "baroque counterpoint and fugue exposition in seventeenth-century harpsichord works",
                "stellar nucleosynthesis in asymptotic giant branch stars",
                "temperate deciduous forest canopy transpiration and stomatal conductance",
                "medieval maritime trade routes across the Mediterranean basin",
                "tectonic subduction zone metamorphism and eclogite facies",
                "classical thermodynamics of Carnot cycles and entropy production",
                "avian migratory navigation via magnetoreception and celestial cues",
                "ancient Mesopotamian cuneiform administrative tablets and grain ledgers",
                "oceanic thermohaline circulation and North Atlantic deep water formation",
                "glaciological mass balance of alpine valley glaciers",
                "phonological vowel shifts in Indo-European comparative linguistics",
                "botanical taxonomy of alpine bryophytes and lichens",
                "fluvial geomorphology of meandering alluvial river channels",
                "acoustic resonance modes in orchestral string instruments",
                "tropospheric Rossby wave propagation and jet stream meanders",
            ]
            * 4
        )
    ]
)


def save_npy_f32(path: Path, matrix: list[list[float]]) -> None:
    """Save a 2D float32 matrix in standard NumPy v1.0 .npy format."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = len(matrix)
    cols = len(matrix[0]) if rows > 0 else 0
    header_dict = f"{{'descr': '<f4', 'fortran_order': False, 'shape': ({rows}, {cols}), }}"
    prefix_len = 10  # 6-byte magic + 2-byte version + 2-byte header len
    pad_len = (64 - ((prefix_len + len(header_dict) + 1) % 64)) % 64
    header_bytes = (header_dict + (" " * pad_len) + "\n").encode("latin1")
    flat = array.array("f", (float(x) for row in matrix for x in row))
    with path.open("wb") as fh:
        fh.write(b"\x93NUMPY\x01\x00")
        fh.write(struct.pack("<H", len(header_bytes)))
        fh.write(header_bytes)
        fh.write(flat.tobytes())


def load_npy_f32(path: Path) -> list[list[float]]:
    """Load a 2D float32 matrix from a standard NumPy v1.0 .npy file."""
    raw = path.read_bytes()
    if raw[:6] != b"\x93NUMPY":
        raise ValueError(f"Invalid .npy header in {path}")
    hlen = struct.unpack("<H", raw[8:10])[0]
    header_str = raw[10 : 10 + hlen].decode("latin1").strip()
    meta = ast.literal_eval(header_str)
    rows, cols = (int(meta["shape"][0]), int(meta["shape"][1]))
    flat = array.array("f")
    flat.frombytes(raw[10 + hlen : 10 + hlen + rows * cols * 4])
    if cols == 0 or rows == 0:
        return []
    return [list(flat[i * cols : (i + 1) * cols]) for i in range(rows)]


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{value:.6f}" for value in values) + "]"


def _git_sha(root: Path) -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return "unknown"


class CachedQueryEmbedder:
    """Embedder wrapper that serves precomputed query vectors and truncates to target dim."""

    def __init__(
        self,
        *,
        name: str,
        kind: str,
        dim: int,
        query_map: dict[str, list[float]],
        fallback: Embedder,
    ) -> None:
        self.name = name
        self.kind = kind
        self.dim = dim
        self._query_map = query_map
        self._fallback = fallback

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vecs = await self._fallback.embed_documents(texts)
        return [_l2_normalize(v[: self.dim]) for v in vecs]

    async def embed_query(self, text: str) -> list[float]:
        vec = self._query_map.get(text)
        if vec is not None:
            return _l2_normalize(vec[: self.dim])
        raw = await self._fallback.embed_query(text)
        return _l2_normalize(raw[: self.dim])

    async def aclose(self) -> None:
        return None


def _load_v1_corpus_and_questions(
    root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    corpus_dir = root / "evals" / "data" / "v1" / "corpus"
    questions_path = root / "evals" / "data" / "v1" / "questions.jsonl"
    corpus_chunks: list[dict[str, Any]] = []
    for file in sorted(corpus_dir.glob("*.md")):
        meta, body = parse_front_matter(file.read_text(encoding="utf-8"))
        doc_key = meta.get("slug") or file.stem
        title = meta.get("title") or file.stem
        collection = meta.get("collection") or "knowledge"
        visibility = meta.get("visibility") or "lab"
        pieces = chunk_markdown(body)
        for piece in pieces:
            corpus_chunks.append(
                {
                    "doc_key": doc_key,
                    "title": title,
                    "collection": collection,
                    "visibility": visibility,
                    "body": body,
                    "chunk_index": int(piece.index),
                    "heading": str(piece.heading),
                    "content": str(piece.text),
                    "tokens": tokenize(str(piece.text)),
                }
            )
    questions = [
        json.loads(line)
        for line in questions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return corpus_chunks, questions


async def _prepare_cached_vectors(
    *,
    settings: Settings,
    cache_dir: Path,
    corpus_chunks: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> tuple[list[list[float]], dict[str, list[float]], dict[str, float], list[list[float]]]:
    """Load or compute corpus, query, and seed distractor vectors, caching .npy files."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    slug = settings.base_embedding_slug
    dim = int(settings.embedding_dim)
    corpus_npy = cache_dir / f"corpus_{slug}_{dim}.npy"
    queries_npy = cache_dir / f"queries_{slug}_{dim}.npy"
    queries_meta = cache_dir / f"queries_{slug}_{dim}.meta.json"
    seeds_npy = cache_dir / f"distractor_seeds_{slug}_{dim}.npy"

    need_embed = not (
        corpus_npy.is_file()
        and queries_npy.is_file()
        and queries_meta.is_file()
        and seeds_npy.is_file()
    )

    if need_embed:
        embedder = build_embedder(settings)
        try:
            corpus_texts = [str(c["content"]) for c in corpus_chunks]
            corpus_vecs = await embedder.embed_documents(corpus_texts)
            save_npy_f32(corpus_npy, corpus_vecs)

            q_vecs: list[list[float]] = []
            q_latencies: list[float] = []
            q_texts: list[str] = []
            for q in questions:
                q_str = str(q["question"])
                q_texts.append(q_str)
                t0 = time.perf_counter()
                vec = await embedder.embed_query(q_str)
                q_latencies.append((time.perf_counter() - t0) * 1000.0)
                q_vecs.append(vec)
            save_npy_f32(queries_npy, q_vecs)
            queries_meta.write_text(
                json.dumps(
                    {"questions": q_texts, "latencies_ms": q_latencies},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            seed_vecs = await embedder.embed_documents(list(DISTRACTOR_SEED_TEXTS))
            save_npy_f32(seeds_npy, seed_vecs)
        finally:
            await embedder.aclose()

    corpus_vecs = load_npy_f32(corpus_npy)
    q_vecs = load_npy_f32(queries_npy)
    q_meta = json.loads(queries_meta.read_text(encoding="utf-8"))
    seed_vecs = load_npy_f32(seeds_npy)

    q_texts = list(q_meta["questions"])
    q_latencies = [float(x) for x in q_meta["latencies_ms"]]
    sorted_q_lat = sorted(q_latencies)
    query_map = {q_texts[i]: q_vecs[i] for i in range(len(q_texts))}
    latency_summary = {
        "mean": round(sum(q_latencies) / max(len(q_latencies), 1), 4),
        "p50": round(_percentile(sorted_q_lat, 0.50), 4),
        "p95": round(_percentile(sorted_q_lat, 0.95), 4),
    }
    return corpus_vecs, query_map, latency_summary, seed_vecs


async def _ensure_eval_user_and_corpus_docs(
    corpus_chunks: list[dict[str, Any]],
    corpus_vecs: list[list[float]],
    *,
    storage: str,
    dim: int,
) -> tuple[Principal, uuid.UUID]:
    """Reset eval documents/chunks, insert v1 corpus chunks and a synthetic distractor document."""
    maker = get_sessionmaker()
    async with maker() as session:
        user = await session.scalar(
            select(User).where(User.email == "admin@lab.local")
        )
        if user is None:
            user = User(
                email="admin@lab.local",
                display_name="沈昭",
                role="admin",
                password_hash=hash_password("LabAgent-Demo-2026-Admin"),
                is_active=True,
            )
            session.add(user)
            await session.flush()

        await session.execute(text("DROP INDEX IF EXISTS chunks_embedding_hnsw"))
        await session.execute(text("DROP INDEX IF EXISTS chunks_embedding_prefix_hnsw"))
        await session.execute(text("DELETE FROM chunks"))
        await session.execute(text("DELETE FROM documents WHERE source = 'eval'"))
        await session.execute(
            text(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE {storage}({dim})")
        )

        insert_chunk_stmt = text(
            f"""
            INSERT INTO chunks (id, document_id, chunk_index, heading, content, tokens, embedding, content_tsv)
            VALUES (
                CAST(:id AS uuid),
                CAST(:document_id AS uuid),
                :chunk_index,
                :heading,
                :content,
                :tokens,
                CAST(:embedding AS {storage}({dim})),
                to_tsvector('simple', :segmented)
            )
            """
        )
        docs_by_key: dict[str, Document] = {}
        for item, full_vec in zip(corpus_chunks, corpus_vecs, strict=True):
            doc_key = str(item["doc_key"])
            doc = docs_by_key.get(doc_key)
            if doc is None:
                doc = Document(
                    owner_id=user.id,
                    title=str(item["title"]),
                    collection=str(item["collection"]),
                    source="eval",
                    source_format="markdown",
                    doc_key=doc_key,
                    visibility=str(item["visibility"]),
                    status="ready",
                    content_text=str(item["body"]),
                )
                session.add(doc)
                await session.flush()
                docs_by_key[doc_key] = doc
            trunc_vec = _l2_normalize(full_vec[:dim])
            tok_list = list(item["tokens"])
            await session.execute(
                insert_chunk_stmt,
                {
                    "id": str(uuid.uuid4()),
                    "document_id": str(doc.id),
                    "chunk_index": int(item["chunk_index"]),
                    "heading": str(item["heading"]),
                    "content": str(item["content"]),
                    "tokens": tok_list,
                    "embedding": _vec_literal(trunc_vec),
                    "segmented": " ".join(tok_list),
                },
            )

        distractor_doc = Document(
            owner_id=user.id,
            title="Synthetic Scale Distractors",
            collection="knowledge",
            source="eval",
            source_format="markdown",
            doc_key="scale-distractors",
            visibility="lab",
            status="ready",
            content_text="Synthetic distractor collection for scale benchmarking.",
        )
        session.add(distractor_doc)
        await session.flush()
        distractor_doc_id = distractor_doc.id
        principal = Principal(id=user.id, role="admin")
        await session.commit()
    return principal, distractor_doc_id


def _build_distractor_pools_and_corpus(
    corpus_vecs: list[list[float]],
    seed_vecs: list[list[float]],
    dim: int,
) -> tuple[list[list[float]], list[list[list[float]]]]:
    """Build distractor components at native_dim and slice to dim so truncated runs are exact MRL prefixes."""
    native_dim = (
        len(corpus_vecs[0])
        if corpus_vecs
        else (len(seed_vecs[0]) if seed_vecs else dim)
    )
    n_seeds = max(len(seed_vecs), 1)
    rng = random.Random(20261106)
    pools: list[list[list[float]]] = []
    for p_idx in range(4):
        pool: list[list[float]] = []
        for idx in range(64):
            s_vec_full = _l2_normalize(
                seed_vecs[(p_idx * 16 + idx) % n_seeds][:native_dim]
            )
            r_vec_full = _l2_normalize(
                [rng.gauss(0.0, 1.0) for _ in range(native_dim)]
            )
            mixed_full = _l2_normalize(
                [
                    0.25 * s_vec_full[j] + 0.968 * r_vec_full[j]
                    for j in range(native_dim)
                ]
            )
            pool.append(mixed_full[:dim])
        pools.append(pool)

    scaled_corpus: list[list[float]] = []
    for cvec in corpus_vecs:
        full_norm = _l2_normalize(cvec[:native_dim])
        scaled_full = [x * 1.15 for x in full_norm]
        scaled_corpus.append(scaled_full[:dim])
    return scaled_corpus, pools


async def _populate_distractors_up_to(
    *,
    distractor_doc_id: uuid.UUID,
    corpus_vecs: list[list[float]],
    seed_vecs: list[list[float]],
    storage: str,
    dim: int,
    target_total_chunks: int,
) -> int:
    """Populate synthetic distractor rows in chunks up to target_total_chunks."""
    maker = get_sessionmaker()
    n_corpus = len(corpus_vecs)
    n_seeds = max(len(seed_vecs), 1)
    scaled_corpus, pools = _build_distractor_pools_and_corpus(
        corpus_vecs, seed_vecs, dim
    )

    async with maker() as session:
        cur_total = int(
            (await session.execute(text("SELECT COUNT(*) FROM chunks"))).scalar_one()
        )
        cur_distractors = int(
            (
                await session.execute(
                    text("SELECT COUNT(*) FROM chunks WHERE document_id = :doc_id"),
                    {"doc_id": str(distractor_doc_id)},
                )
            ).scalar_one()
        )
        needed = max(0, target_total_chunks - cur_total)
        if needed == 0:
            return cur_total

        await session.execute(
            text(
                f"CREATE TEMP TABLE IF NOT EXISTS _bench_corpus (id int PRIMARY KEY, v {storage}({dim})) ON COMMIT DROP"
            )
        )
        await session.execute(text("TRUNCATE _bench_corpus"))
        for idx, scaled in enumerate(scaled_corpus):
            await session.execute(
                text(
                    f"INSERT INTO _bench_corpus (id, v) VALUES (:id, CAST(:v AS {storage}({dim})))"
                ),
                {"id": idx, "v": _vec_literal(scaled)},
            )

        await session.execute(
            text(
                f"CREATE TEMP TABLE IF NOT EXISTS _bench_pools (pool int, id int, v {storage}({dim}), PRIMARY KEY (pool, id)) ON COMMIT DROP"
            )
        )
        await session.execute(text("TRUNCATE _bench_pools"))
        for p_idx, pool in enumerate(pools, start=1):
            for idx, pvec in enumerate(pool):
                await session.execute(
                    text(
                        f"INSERT INTO _bench_pools (pool, id, v) VALUES (:pool, :id, CAST(:v AS {storage}({dim})))"
                    ),
                    {"pool": p_idx, "id": idx, "v": _vec_literal(pvec)},
                )

        start_idx = cur_distractors + 1
        end_idx = cur_distractors + needed
        await session.execute(
            text(
                """
                INSERT INTO chunks (
                    id,
                    document_id,
                    chunk_index,
                    heading,
                    content,
                    content_tsv,
                    tokens,
                    embedding
                )
                SELECT gen_random_uuid(),
                       CAST(:doc_id AS uuid),
                       g,
                       'Distractor Section ' || g::text,
                       'Synthetic scale distractor passage ' || g::text || ' topic_' || (g % :n_seeds)::text,
                       to_tsvector('simple', 'distractor ' || g::text || ' topic_' || (g % :n_seeds)::text),
                       ARRAY['distractor', 'topic_' || (g % :n_seeds)::text]::text[],
                       l2_normalize(tc.v + p1.v + p2.v + p3.v + p4.v)
                FROM generate_series(CAST(:start_idx AS int), CAST(:end_idx AS int)) AS g
                JOIN _bench_corpus tc ON tc.id = (g % :n_corpus)
                JOIN _bench_pools p1 ON p1.pool = 1 AND p1.id = (g % 64)
                JOIN _bench_pools p2 ON p2.pool = 2 AND p2.id = ((g * 3 + g / 64) % 64)
                JOIN _bench_pools p3 ON p3.pool = 3 AND p3.id = ((g * 7 + (g / 64) * 13 + g / 4096) % 64)
                JOIN _bench_pools p4 ON p4.pool = 4 AND p4.id = ((g * 19 + (g / 64) * 29 + (g / 4096) * 5) % 64)
                """
            ),
            {
                "doc_id": str(distractor_doc_id),
                "start_idx": start_idx,
                "end_idx": end_idx,
                "n_corpus": n_corpus,
                "n_seeds": n_seeds,
            },
        )
        await session.commit()
        await session.execute(text("ANALYZE documents"))
        await session.execute(text("ANALYZE chunks"))
        await session.commit()
        final_total = int(
            (await session.execute(text("SELECT COUNT(*) FROM chunks"))).scalar_one()
        )
        return final_total


async def _write_index_meta(session: Any, settings: Settings) -> None:
    model_name = (
        "hashing-cjk-v1" if settings.embedder == "hashing" else settings.embedding_model
    )
    index_kind = settings.resolved_embedding_index
    if index_kind == "hnsw_prefix":
        await session.execute(
            text(
                "ALTER TABLE embedding_index_meta "
                "DROP CONSTRAINT IF EXISTS embedding_index_meta_index_kind"
            )
        )
        await session.execute(
            text(
                "ALTER TABLE embedding_index_meta "
                "ADD CONSTRAINT embedding_index_meta_index_kind "
                "CHECK (index_kind IN ('hnsw', 'hnsw_prefix', 'none'))"
            )
        )
    await session.execute(
        text(
            """
            INSERT INTO embedding_index_meta (id, fingerprint, embedder, model, dim, storage, index_kind, updated_at)
            VALUES (1, :fingerprint, :embedder, :model, :dim, :storage, :index_kind, now())
            ON CONFLICT (id) DO UPDATE SET
                fingerprint = EXCLUDED.fingerprint,
                embedder = EXCLUDED.embedder,
                model = EXCLUDED.model,
                dim = EXCLUDED.dim,
                storage = EXCLUDED.storage,
                index_kind = EXCLUDED.index_kind,
                updated_at = now()
            """
        ),
        {
            "fingerprint": settings.embedding_fingerprint,
            "embedder": settings.embedder,
            "model": model_name,
            "dim": int(settings.embedding_dim),
            "storage": settings.embedding_storage,
            "index_kind": index_kind,
        },
    )


async def _evaluate_path(
    *,
    settings: Settings,
    embedder: Embedder,
    user: Principal,
    questions: list[dict[str, Any]],
    corpus_chunks: list[dict[str, Any]],
    query_embed_latency_ms: dict[str, float],
) -> dict[str, Any]:
    """Run search_knowledge across all v1 questions and compute retrieval + latency metrics."""
    heading_to_key: dict[tuple[str, str], str] = {}
    for c in corpus_chunks:
        heading_to_key[(str(c["doc_key"]), str(c["heading"]))] = (
            f"{c['doc_key']}#{c['chunk_index']}"
        )

    mode = str(settings.retrieval_mode or "hybrid_rrf")
    params = {
        "rrf_k": int(settings.rrf_k or 60),
        "evidence_score_min": float(settings.evidence_score_min or 0.5),
        "rare_coverage_min": float(settings.rare_coverage_min or 0.0),
        "cite_margin": float(settings.cite_margin or 0.15),
        "cite_k": int(settings.cite_k or 3),
    }

    maker = get_sessionmaker()
    rows: list[dict[str, Any]] = []
    async with maker() as session:
        # Warm-up query so connection/cache initialization does not skew p95
        if questions:
            await search_knowledge(
                session,
                settings=settings,
                embedder=embedder,
                user=user,
                query=str(questions[0]["question"]),
                mode="dense",
                source="eval",
                limit=None,
            )
        for q in questions:
            bundle = await search_knowledge(
                session,
                settings=settings,
                embedder=embedder,
                user=user,
                query=str(q["question"]),
                mode="dense",
                source="eval",
                limit=None,
            )
            gold_keys = [
                heading_to_key[(g["doc"], g["heading"])]
                for g in q.get("gold", [])
                if (g["doc"], g["heading"]) in heading_to_key
            ]
            rows.append(
                {
                    "id": q["id"],
                    "split": q["split"],
                    "type": q["type"],
                    "answerable": bool(q["answerable"]),
                    "gold": gold_keys,
                    "query_tokens": content_tokens(str(q["question"])),
                    "elapsed_ms": bundle.elapsed_ms,
                    "df": bundle.df,
                    "n_docs": bundle.n_docs,
                    "hits": [
                        {
                            "key": h["key"],
                            "cosine": round(float(h.get("cosine", 0.0)), 6),
                            "bm25": round(float(h.get("bm25", 0.0)), 6),
                            "tokens": h.get("tokens", []),
                        }
                        for h in bundle.hits
                    ],
                }
            )
        bytes_row = await session.execute(
            text("SELECT pg_total_relation_size('chunks')")
        )
        chunks_table_bytes = int(bytes_row.scalar_one() or 0)

    all_latencies = [float(r["elapsed_ms"]) for r in rows]
    sorted_lat = sorted(all_latencies)
    dev_rows = [r for r in rows if r["split"] == "dev"]
    test_rows = [r for r in rows if r["split"] == "test"]
    all_metrics = evaluate(rows, mode=mode, params=params, include_ci=False)
    dev_metrics = evaluate(dev_rows, mode=mode, params=params, include_ci=False)
    test_metrics = evaluate(test_rows, mode=mode, params=params, include_ci=False)

    db_lat = {
        "mean": round(sum(all_latencies) / max(len(all_latencies), 1), 4),
        "p50": round(_percentile(sorted_lat, 0.50), 4),
        "p95": round(_percentile(sorted_lat, 0.95), 4),
    }
    e2e_lat = {
        "mean": round(db_lat["mean"] + float(query_embed_latency_ms["mean"]), 4),
        "p50": round(db_lat["p50"] + float(query_embed_latency_ms["p50"]), 4),
        "p95": round(db_lat["p95"] + float(query_embed_latency_ms["p95"]), 4),
    }

    return {
        "fingerprint": settings.embedding_fingerprint,
        "storage": f"{settings.embedding_storage}({settings.embedding_dim})",
        "index_kind": settings.resolved_embedding_index,
        "selected_mode": mode,
        "chunks_table_bytes": chunks_table_bytes,
        "db_search_latency_ms": db_lat,
        "total_search_latency_ms": e2e_lat,
        "all": {
            "recall_at_5": round(metric_value(all_metrics["recall_at_5"]), 4),
            "mrr": round(metric_value(all_metrics["mrr"]), 4),
            "citation_f1": round(metric_value(all_metrics["citation_f1"]), 4),
            "refusal_correctness": round(
                metric_value(all_metrics["refusal_correctness"]), 4
            ),
        },
        "dev": {
            "recall_at_5": round(metric_value(dev_metrics["recall_at_5"]), 4),
            "mrr": round(metric_value(dev_metrics["mrr"]), 4),
            "citation_f1": round(metric_value(dev_metrics["citation_f1"]), 4),
            "refusal_correctness": round(
                metric_value(dev_metrics["refusal_correctness"]), 4
            ),
        },
        "test": {
            "recall_at_5": round(metric_value(test_metrics["recall_at_5"]), 4),
            "mrr": round(metric_value(test_metrics["mrr"]), 4),
            "citation_f1": round(metric_value(test_metrics["citation_f1"]), 4),
            "refusal_correctness": round(
                metric_value(test_metrics["refusal_correctness"]), 4
            ),
            "latency_ms_p50": round(float(test_metrics["latency_ms_p50"]), 4),
            "latency_ms_p95": round(float(test_metrics["latency_ms_p95"]), 4),
        },
    }


def _make_path_settings(base: Settings, **overrides: Any) -> Settings:
    params: dict[str, Any] = {
        "_env_file": None,
        "app_env": base.app_env,
        "database_url": base.database_url,
        "redis_url": base.redis_url,
        "jwt_secret": base.jwt_secret.get_secret_value(),
        "cors_origins": base.cors_origins,
        "embedder": base.embedder,
        "embedding_base_url": base.embedding_base_url,
        "embedding_model": base.embedding_model,
        "embedding_model_version": base.embedding_model_version,
        "embedding_dim": base.embedding_dim,
        "embedding_native_dim": base.embedding_native_dim,
        "embedding_truncate": base.embedding_truncate,
        "embedding_storage": base.embedding_storage,
        "embedding_index": base.embedding_index,
        "embedding_prefix_dim": base.embedding_prefix_dim,
        "embedding_query_template": base.embedding_query_template,
        "embedding_query_instruction": base.embedding_query_instruction,
        "embedding_document_prefix": base.embedding_document_prefix,
        "embedding_redact": base.embedding_redact,
        "load_tuned_config": True,
    }
    params.update(overrides)
    return Settings(**params)


def _resolve_benchmark_base_settings(
    base_env: Settings, embedder_kind: str | None = None
) -> tuple[Settings, str, int, int, int]:
    """Resolve base Settings and (chosen_embedder, full_dim, prefix_dim, trunc_dim) for benchmark."""
    chosen_embedder = embedder_kind or base_env.embedder
    if chosen_embedder == "openai_compatible":
        full_dim = 4096
        prefix_dim = 1024
        trunc_dim = 1024
        base_settings = _make_path_settings(
            base_env,
            embedder="openai_compatible",
            embedding_base_url=base_env.embedding_base_url
            or "http://127.0.0.1:11434/v1",
            embedding_model=(
                base_env.embedding_model
                if base_env.embedding_model != "BAAI/bge-small-zh-v1.5"
                else "qwen3-embedding:8b"
            ),
            embedding_model_version=base_env.embedding_model_version or "64b933495768",
            embedding_dim=full_dim,
            embedding_native_dim=full_dim,
            embedding_truncate=False,
            embedding_storage="halfvec",
            embedding_index="none",
            embedding_prefix_dim=prefix_dim,
            embedding_query_template=(
                base_env.embedding_query_template
                if base_env.embedding_query_template != "{query}"
                else "Instruct: {instruction}\nQuery:{query}"
            ),
            embedding_query_instruction=(
                base_env.embedding_query_instruction
                or "Given a question from a research lab member, retrieve the lab document passage that answers it"
            ),
        )
    elif chosen_embedder == "bge":
        full_dim = int(base_env.embedding_dim) if base_env.embedder == "bge" else 512
        prefix_dim = max(64, full_dim // 4)
        trunc_dim = prefix_dim
        base_settings = _make_path_settings(
            base_env,
            embedder="bge",
            embedding_model=base_env.embedding_model or "BAAI/bge-small-zh-v1.5",
            embedding_dim=full_dim,
            embedding_native_dim=full_dim,
            embedding_truncate=False,
            embedding_storage="halfvec",
            embedding_index="none",
            embedding_prefix_dim=prefix_dim,
        )
    else:
        full_dim = 512
        prefix_dim = 128
        trunc_dim = 128
        base_settings = _make_path_settings(
            base_env,
            embedder="hashing",
            embedding_dim=full_dim,
            embedding_native_dim=None,
            embedding_truncate=False,
            embedding_storage="halfvec",
            embedding_index="none",
            embedding_prefix_dim=prefix_dim,
        )
    return base_settings, chosen_embedder, full_dim, prefix_dim, trunc_dim


async def run_scale_benchmark(
    *,
    chunks: int = 20000,
    embedder_kind: str | None = None,
    output_path: Path | None = None,
    cache_dir: Path | None = None,
    sweep_counts: list[int] | None = None,
) -> dict[str, Any]:
    """Execute the Spec T3.7 scale benchmark across exact, two-stage prefix HNSW, and truncated 1024 HNSW."""
    reset_settings_cache()
    reset_pgvector_version_cache()
    root = repo_root()
    resolved_cache_dir = cache_dir or (root / ".cache" / "bench_scale")
    resolved_output = output_path or (
        root / "evals" / "results" / "scale" / f"bench_scale_{chunks}.json"
    )

    base_env = get_settings()
    base_settings, chosen_embedder, full_dim, prefix_dim, trunc_dim = (
        _resolve_benchmark_base_settings(base_env, embedder_kind)
    )

    corpus_chunks, questions = _load_v1_corpus_and_questions(root)
    corpus_vecs, query_map, query_embed_lat, seed_vecs = await _prepare_cached_vectors(
        settings=base_settings,
        cache_dir=resolved_cache_dir,
        corpus_chunks=corpus_chunks,
        questions=questions,
    )

    fallback_embedder = HashingEmbedder(full_dim)
    cached_full_embedder = CachedQueryEmbedder(
        name=base_settings.embedding_model,
        kind=base_settings.embedder,
        dim=full_dim,
        query_map=query_map,
        fallback=fallback_embedder,
    )
    cached_trunc_embedder = CachedQueryEmbedder(
        name=base_settings.embedding_model,
        kind=base_settings.embedder,
        dim=trunc_dim,
        query_map=query_map,
        fallback=fallback_embedder,
    )

    raw_sweep = sweep_counts or [1000, 5000, 10000, chunks]
    active_sweep = sorted({c for c in raw_sweep if c <= chunks} | {chunks})

    maker = get_sessionmaker()
    try:
        # 1. Exact 4096h scan + chunk count latency sweep
        exact_settings = _make_path_settings(
            base_settings,
            embedding_dim=full_dim,
            embedding_storage="halfvec",
            embedding_index="none",
        )
        user, distractor_doc_id = await _ensure_eval_user_and_corpus_docs(
            corpus_chunks,
            corpus_vecs,
            storage="halfvec",
            dim=full_dim,
        )
        async with maker() as session:
            await _write_index_meta(session, exact_settings)
            await session.commit()

        sweep_records: list[dict[str, Any]] = []
        exact_result: dict[str, Any] = {}
        for target_count in active_sweep:
            actual_count = await _populate_distractors_up_to(
                distractor_doc_id=distractor_doc_id,
                corpus_vecs=corpus_vecs,
                seed_vecs=seed_vecs,
                storage="halfvec",
                dim=full_dim,
                target_total_chunks=target_count,
            )
            step_eval = await _evaluate_path(
                settings=exact_settings,
                embedder=cached_full_embedder,
                user=user,
                questions=questions,
                corpus_chunks=corpus_chunks,
                query_embed_latency_ms=query_embed_lat,
            )
            sweep_records.append(
                {
                    "chunks": actual_count,
                    "db_search_p50_ms": step_eval["db_search_latency_ms"]["p50"],
                    "db_search_p95_ms": step_eval["db_search_latency_ms"]["p95"],
                    "total_search_p50_ms": step_eval["total_search_latency_ms"]["p50"],
                    "total_search_p95_ms": step_eval["total_search_latency_ms"]["p95"],
                    "recall_at_5_all": step_eval["all"]["recall_at_5"],
                    "recall_at_5_test": step_eval["test"]["recall_at_5"],
                }
            )
            if target_count == chunks:
                exact_result = step_eval

        # 2. Two-stage prefix HNSW on the exact same 20k chunks table
        prefix_settings = _make_path_settings(
            base_settings,
            embedding_dim=full_dim,
            embedding_storage="halfvec",
            embedding_index="hnsw_prefix",
            embedding_prefix_dim=prefix_dim,
        )
        t_idx0 = time.perf_counter()
        async with maker() as session:
            await session.execute(text("SET LOCAL maintenance_work_mem = '512MB'"))
            await session.execute(
                text(
                    f"CREATE INDEX chunks_embedding_prefix_hnsw ON chunks USING hnsw "
                    f"((subvector(embedding, 1, {prefix_dim})::halfvec({prefix_dim})) halfvec_cosine_ops)"
                )
            )
            await session.execute(text("ANALYZE chunks"))
            await _write_index_meta(session, prefix_settings)
            await session.commit()
            assert await check_index(session, prefix_settings) == "ok"
        prefix_index_build_s = round(time.perf_counter() - t_idx0, 4)

        two_stage_result = await _evaluate_path(
            settings=prefix_settings,
            embedder=cached_full_embedder,
            user=user,
            questions=questions,
            corpus_chunks=corpus_chunks,
            query_embed_latency_ms=query_embed_lat,
        )
        two_stage_result["index_build_seconds"] = prefix_index_build_s

        # 3. Truncated 1024-dim vector HNSW
        trunc_settings = _make_path_settings(
            base_settings,
            embedding_dim=trunc_dim,
            embedding_native_dim=full_dim if chosen_embedder != "hashing" else None,
            embedding_truncate=chosen_embedder != "hashing",
            embedding_storage="vector",
            embedding_index="hnsw",
        )
        user, distractor_doc_id = await _ensure_eval_user_and_corpus_docs(
            corpus_chunks,
            corpus_vecs,
            storage="vector",
            dim=trunc_dim,
        )
        await _populate_distractors_up_to(
            distractor_doc_id=distractor_doc_id,
            corpus_vecs=corpus_vecs,
            seed_vecs=seed_vecs,
            storage="vector",
            dim=trunc_dim,
            target_total_chunks=chunks,
        )
        t_idx1 = time.perf_counter()
        async with maker() as session:
            await session.execute(text("SET LOCAL maintenance_work_mem = '512MB'"))
            await session.execute(
                text(
                    "CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
                )
            )
            await session.execute(text("ANALYZE chunks"))
            await _write_index_meta(session, trunc_settings)
            await session.commit()
            assert await check_index(session, trunc_settings) == "ok"
        trunc_index_build_s = round(time.perf_counter() - t_idx1, 4)

        trunc_result = await _evaluate_path(
            settings=trunc_settings,
            embedder=cached_trunc_embedder,
            user=user,
            questions=questions,
            corpus_chunks=corpus_chunks,
            query_embed_latency_ms=query_embed_lat,
        )
        trunc_result["index_build_seconds"] = trunc_index_build_s

        # Compute empirical exact-scan threshold for p95 <= 300 ms
        max_chunks_under_300ms_total = max(
            (r["chunks"] for r in sweep_records if r["total_search_p95_ms"] <= 300.0),
            default=0,
        )
        max_chunks_under_300ms_db = max(
            (r["chunks"] for r in sweep_records if r["db_search_p95_ms"] <= 300.0),
            default=0,
        )

        exact_r5_all = float(exact_result["all"]["recall_at_5"])
        two_stage_r5_all = float(two_stage_result["all"]["recall_at_5"])
        exact_r5_test = float(exact_result["test"]["recall_at_5"])
        two_stage_r5_test = float(two_stage_result["test"]["recall_at_5"])
        ratio_all = (
            round(two_stage_r5_all / exact_r5_all, 4) if exact_r5_all > 0 else 1.0
        )
        ratio_test = (
            round(two_stage_r5_test / exact_r5_test, 4) if exact_r5_test > 0 else 1.0
        )

        report: dict[str, Any] = {
            "suite": "scale",
            "dataset": "v1",
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "git_sha": _git_sha(root),
            "target_chunks": chunks,
            "corpus_chunks": len(corpus_chunks),
            "distractor_chunks": max(0, chunks - len(corpus_chunks)),
            "embedder": base_settings.embedder,
            "embedding_model": base_settings.embedding_model,
            "query_embedding_latency_ms": query_embed_lat,
            "exact_scan_sweep": sweep_records,
            "exact_scan_p95_le_300ms_max_chunks": {
                "db_search_only": max_chunks_under_300ms_db,
                "total_with_query_embedding": max_chunks_under_300ms_total,
            },
            "two_stage_vs_exact": {
                "recall_at_5_ratio_all": ratio_all,
                "recall_at_5_ratio_test": ratio_test,
                "meets_098_target": (
                    ratio_all >= 0.98 and ratio_test >= 0.98
                ),
            },
            "known_limitations": [
                "BM25 document frequencies (df) and average document length (avgdl) are computed over the candidate pool (dense_pool + lexical_pool) rather than the full chunks table; at 20k+ chunks this shifts relative BM25 IDF weights within the candidate set."
            ],
            "paths": {
                "exact_4096h": exact_result,
                "two_stage_prefix1024": two_stage_result,
                "truncated_1024_hnsw": trunc_result,
            },
        }
        resolved_output.parent.mkdir(parents=True, exist_ok=True)
        resolved_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return report
    finally:
        restore_settings = _make_path_settings(base_env)
        restore_embedder = build_embedder(restore_settings)
        try:
            async with maker() as session:
                await session.execute(
                    text("DELETE FROM documents WHERE source = 'eval'")
                )
                await session.commit()
                rebuilt = await apply_reindex(
                    session,
                    restore_settings,
                    restore_embedder,
                    if_needed=False,
                    force=True,
                )
            if rebuilt:
                while (
                    await process_next_pending(restore_embedder, restore_settings)
                    is not None
                ):
                    pass
        finally:
            await restore_embedder.aclose()
            await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark retrieval scale at 20k chunks across exact, two-stage prefix HNSW, and truncated HNSW."
    )
    parser.add_argument(
        "--chunks",
        type=int,
        default=20000,
        help="Total number of chunks in chunks table (default: 20000)",
    )
    parser.add_argument(
        "--embedder",
        choices=("hashing", "openai_compatible", "bge"),
        default=None,
        help="Embedder backend to benchmark (default: from environment)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output JSON report path (default: evals/results/scale/bench_scale_<chunks>.json)",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help="Directory for cached .npy vectors (default: .cache/bench_scale)",
    )
    parser.add_argument(
        "--sweep-counts",
        type=str,
        default="1000,5000,10000,20000",
        help="Comma-separated chunk counts for exact scan latency curve",
    )
    args = parser.parse_args(argv)
    sweep = [int(x.strip()) for x in args.sweep_counts.split(",") if x.strip()]
    report = asyncio.run(
        run_scale_benchmark(
            chunks=args.chunks,
            embedder_kind=args.embedder,
            output_path=Path(args.output) if args.output else None,
            cache_dir=Path(args.cache_dir) if args.cache_dir else None,
            sweep_counts=sweep,
        )
    )
    if not report["two_stage_vs_exact"]["meets_098_target"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
