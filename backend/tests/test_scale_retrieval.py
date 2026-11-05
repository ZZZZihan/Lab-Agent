"""Tests for Spec T3.7 / PR-27: HNSW runtime tuning, two-stage prefix HNSW, and evals.bench_scale."""

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.acl import Principal
from app.auth import hash_password
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.ingest_worker import process_next_pending
from app.models import Document, User
from app.rag.embedder import Embedder, HashingEmbedder, OpenAICompatibleEmbedder
from app.rag.index_meta import (
    actual_index_kind,
    check_index,
    column_type,
    has_hnsw_index,
    has_prefix_hnsw_index,
    read_meta,
)
from app.rag.retrieve import (
    _parse_pgvector_version,
    get_pgvector_version,
    reset_pgvector_version_cache,
    search_knowledge,
)
from app.reindex import apply_reindex
from evals.bench_scale import (
    _build_distractor_pools_and_corpus,
    _evaluate_path,
    _make_path_settings,
    _prepare_cached_vectors,
    _resolve_benchmark_base_settings,
    run_scale_benchmark,
    save_npy_f32,
)
from sqlalchemy import text

JWT_SECRET = "test-only-jwt-secret-value-0123456789-abcdef"


def _settings(**overrides: Any) -> Settings:
    base = get_settings()
    params: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "database_url": base.database_url,
        "redis_url": base.redis_url,
        "jwt_secret": JWT_SECRET,
        "cors_origins": "http://127.0.0.1:43123",
        "load_tuned_config": False,
        "embedder": "hashing",
        "embedding_model": "BAAI/bge-small-zh-v1.5",
        "embedding_dim": 512,
        "embedding_storage": "vector",
        "embedding_index": "auto",
    }
    params.update(overrides)
    return Settings(**params)


async def _drain_pending(embedder: Embedder, settings: Settings) -> None:
    while await process_next_pending(embedder, settings) is not None:
        pass


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 1e-12:
        return list(vec)
    return [x / norm for x in vec]


async def test_pgvector_version_detection_and_caching() -> None:
    assert _parse_pgvector_version("0.8.0") == (0, 8, 0)
    assert _parse_pgvector_version("0.7.4") == (0, 7, 4)
    assert _parse_pgvector_version("0.8.1-devel") == (0, 8, 1)
    assert _parse_pgvector_version("") == (0, 0)
    assert _parse_pgvector_version(None) == (0, 0)

    reset_pgvector_version_cache()
    maker = get_sessionmaker()
    async with maker() as session:
        version_first = await get_pgvector_version(session)
        version_second = await get_pgvector_version(session)
        assert version_first == version_second
        assert version_first >= (0, 8)


async def test_hnsw_session_parameters_ef_search_and_iterative_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_pgvector_version_cache()
    maker = get_sessionmaker()
    user_id = uuid.uuid4()
    async with maker() as session:
        user = User(
            id=user_id,
            email="admin@lab.local",
            display_name="沈昭",
            role="admin",
            password_hash=hash_password("LabAgent-Demo-2026-Admin"),
            is_active=True,
        )
        session.add(user)
        await session.commit()

    s_hnsw = _settings(
        embedder="hashing",
        embedding_dim=512,
        embedding_storage="vector",
        embedding_index="hnsw",
        dense_pool=120,
    )
    async with maker() as session:
        await search_knowledge(
            session,
            settings=s_hnsw,
            embedder=HashingEmbedder(512),
            user=Principal(id=user_id, role="admin"),
            query="清衡集群",
        )
        ef_val = (await session.execute(text("SHOW hnsw.ef_search"))).scalar_one()
        iter_val = (await session.execute(text("SHOW hnsw.iterative_scan"))).scalar_one()
        assert int(ef_val) == 240
        assert str(iter_val) == "relaxed_order"

    # When pgvector < 0.8, hnsw.iterative_scan should not be enabled
    async def _fake_old_pgvector(_session: Any) -> tuple[int, ...]:
        return (0, 7, 4)

    monkeypatch.setattr("app.rag.retrieve.get_pgvector_version", _fake_old_pgvector)
    s_small_pool = _settings(
        embedder="hashing",
        embedding_dim=512,
        embedding_storage="vector",
        embedding_index="hnsw",
        dense_pool=30,
    )
    async with maker() as session:
        await search_knowledge(
            session,
            settings=s_small_pool,
            embedder=HashingEmbedder(512),
            user=Principal(id=user_id, role="admin"),
            query="清衡集群",
        )
        ef_val2 = (await session.execute(text("SHOW hnsw.ef_search"))).scalar_one()
        iter_val2 = (await session.execute(text("SHOW hnsw.iterative_scan"))).scalar_one()
        assert int(ef_val2) == 100
        assert str(iter_val2) == "off"


def test_hnsw_prefix_config_fingerprint_and_calibration_fallback() -> None:
    s_exact = _settings(
        embedder="openai_compatible",
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_model="qwen3-embedding:8b",
        embedding_model_version="64b933495768",
        embedding_dim=4096,
        embedding_native_dim=4096,
        embedding_storage="halfvec",
        embedding_index="none",
        embedding_query_template="Instruct: {instruction}\nQuery:{query}",
        embedding_query_instruction="Given a question from a research lab member, retrieve the lab document passage that answers it",
        load_tuned_config=True,
    )
    s_prefix = _settings(
        embedder="openai_compatible",
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_model="qwen3-embedding:8b",
        embedding_model_version="64b933495768",
        embedding_dim=4096,
        embedding_native_dim=4096,
        embedding_storage="halfvec",
        embedding_index="hnsw_prefix",
        embedding_prefix_dim=1024,
        embedding_query_template="Instruct: {instruction}\nQuery:{query}",
        embedding_query_instruction="Given a question from a research lab member, retrieve the lab document passage that answers it",
        load_tuned_config=True,
    )
    assert s_prefix.resolved_embedding_index == "hnsw_prefix"
    assert s_prefix.embedding_fingerprint == f"{s_exact.embedding_fingerprint}|ix=prefix1024"
    assert s_prefix.base_embedding_fingerprint == s_exact.embedding_fingerprint
    assert s_prefix.base_embedding_slug == s_exact.embedding_slug
    # Reuses E1's calibrated thresholds via base_embedding_slug fallback
    assert s_prefix.retrieval_mode == s_exact.retrieval_mode
    assert s_prefix.evidence_score_min == pytest.approx(s_exact.evidence_score_min)
    assert s_prefix.cite_margin == pytest.approx(s_exact.cite_margin)

    with pytest.raises(RuntimeError, match="EMBEDDING_PREFIX_DIM"):
        _settings(
            embedder="hashing",
            embedding_dim=512,
            embedding_storage="halfvec",
            embedding_index="hnsw_prefix",
            embedding_prefix_dim=1024,
        )


async def test_hnsw_prefix_reindex_check_index_and_two_stage_accuracy() -> None:
    maker = get_sessionmaker()
    user_id = uuid.uuid4()
    doc_a_id = uuid.uuid4()
    doc_b_id = uuid.uuid4()

    # Construct 4096-dim vectors where:
    # - Query has weight in prefix (dims 0..1) AND strong weight in tail (dims 1024..1025).
    # - Doc A matches prefix slightly better than Doc B, but has 0 in tail (1024..4095).
    # - Doc B matches prefix well enough to enter stage-1 pool AND matches tail strongly,
    #   so full 4096-dim cosine ranks Doc B > Doc A.
    q_raw = [0.6, 0.0] + [0.0] * 1022 + [0.8, 0.0] + [0.0] * 3070
    a_raw = [1.0, 0.0] + [0.0] * 1022 + [0.0, 0.0] + [0.0] * 3070
    b_raw = [0.55, 0.2] + [0.0] * 1022 + [0.81, 0.0] + [0.0] * 3070
    q_vec = _l2_normalize(q_raw)
    a_vec = _l2_normalize(a_raw)
    b_vec = _l2_normalize(b_raw)

    def handler_4096(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        data = []
        for idx, text_in in enumerate(inputs):
            if "文档A" in text_in:
                vec = a_vec
            elif "文档B" in text_in:
                vec = b_vec
            else:
                vec = q_vec
            data.append({"index": idx, "embedding": vec})
        return httpx.Response(200, json={"data": data})

    s_prefix = _settings(
        embedder="openai_compatible",
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_model="qwen3-embedding:8b",
        embedding_model_version="64b933495768",
        embedding_dim=4096,
        embedding_native_dim=4096,
        embedding_storage="halfvec",
        embedding_index="hnsw_prefix",
        embedding_prefix_dim=1024,
        retrieval_mode="dense",
    )
    s_exact = _settings(
        embedder="openai_compatible",
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_model="qwen3-embedding:8b",
        embedding_model_version="64b933495768",
        embedding_dim=4096,
        embedding_native_dim=4096,
        embedding_storage="halfvec",
        embedding_index="none",
        retrieval_mode="dense",
    )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler_4096))
    emb = OpenAICompatibleEmbedder(s_prefix, client=client)

    try:
        async with maker() as session:
            user = User(
                id=user_id,
                email="admin@lab.local",
                display_name="沈昭",
                role="admin",
                password_hash=hash_password("LabAgent-Demo-2026-Admin"),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            session.add_all(
                [
                    Document(
                        id=doc_a_id,
                        owner_id=user_id,
                        title="前缀优先文档A",
                        collection="knowledge",
                        source="seed",
                        source_format="markdown",
                        doc_key="doc-a",
                        visibility="lab",
                        status="ready",
                        content_text="## 章节A\n\n这是文档A的内容。",
                    ),
                    Document(
                        id=doc_b_id,
                        owner_id=user_id,
                        title="全维最优文档B",
                        collection="knowledge",
                        source="seed",
                        source_format="markdown",
                        doc_key="doc-b",
                        visibility="lab",
                        status="ready",
                        content_text="## 章节B\n\n这是文档B的内容。",
                    ),
                ]
            )
            await session.commit()

        async with maker() as session:
            assert await check_index(session, s_prefix) == "mismatch"
            rebuilt = await apply_reindex(session, s_prefix, emb, if_needed=True)
            assert rebuilt is True
            await _drain_pending(emb, s_prefix)
            assert await check_index(session, s_prefix) == "ok"
            assert await column_type(session) == ("halfvec", 4096)
            assert await actual_index_kind(session) == "hnsw_prefix"
            assert await has_prefix_hnsw_index(session) is True
            assert await has_hnsw_index(session) is True
            meta = await read_meta(session)
            assert meta is not None
            assert meta.index_kind == "hnsw_prefix"
            assert meta.fingerprint.endswith("|ix=prefix1024")
            # Mismatched index_kind setting is detected by check_index
            assert await check_index(session, s_exact) == "mismatch"

            idx_row = await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'chunks' AND indexname = 'chunks_embedding_prefix_hnsw'"
                )
            )
            idx_def = str(idx_row.scalar_one())
            assert "subvector(embedding, 1, 1024)" in idx_def
            assert "halfvec_cosine_ops" in idx_def

            bundle_prefix = await search_knowledge(
                session,
                settings=s_prefix,
                embedder=emb,
                user=Principal(id=user_id, role="admin"),
                query="全维检索测试",
                mode="dense",
            )
            bundle_exact = await search_knowledge(
                session,
                settings=s_exact,
                embedder=emb,
                user=Principal(id=user_id, role="admin"),
                query="全维检索测试",
                mode="dense",
            )
            assert [h["key"] for h in bundle_prefix.hits] == ["doc-b#0", "doc-a#0"]
            assert [h["key"] for h in bundle_exact.hits] == ["doc-b#0", "doc-a#0"]
            assert bundle_prefix.hits[0]["cosine"] == pytest.approx(
                bundle_exact.hits[0]["cosine"], abs=1e-4
            )
            assert bundle_prefix.hits[0]["cosine"] > bundle_prefix.hits[1]["cosine"]
    finally:
        await client.aclose()
        s_restore = _settings(embedder="hashing", embedding_dim=512, embedding_storage="vector")
        async with maker() as session:
            await apply_reindex(
                session, s_restore, HashingEmbedder(512), if_needed=False, force=True
            )


async def test_bench_scale_mrl_distractors_bge_and_sorted_percentiles(
    tmp_path: Path,
) -> None:
    # 1. Verify BGE embedder configuration in _resolve_benchmark_base_settings
    s_default = _settings(embedder="hashing", embedding_dim=512)
    bge_settings, chosen, full_dim, prefix_dim, trunc_dim = (
        _resolve_benchmark_base_settings(s_default, "bge")
    )
    assert chosen == "bge"
    assert bge_settings.embedder == "bge"
    assert bge_settings.embedding_model == "BAAI/bge-small-zh-v1.5"
    assert full_dim == 512
    assert prefix_dim == 128
    assert trunc_dim == 128

    # 2. Verify MRL distractor prefix consistency between full_dim (512) and trunc_dim (128)
    corpus_vecs = [
        _l2_normalize([math.sin((i + 1) * (j + 1) * 0.03) for j in range(512)])
        for i in range(8)
    ]
    seed_vecs = [
        _l2_normalize([math.cos((i + 1) * (j + 1) * 0.05) for j in range(512)])
        for i in range(16)
    ]
    tc_512, pools_512 = _build_distractor_pools_and_corpus(corpus_vecs, seed_vecs, 512)
    tc_128, pools_128 = _build_distractor_pools_and_corpus(corpus_vecs, seed_vecs, 128)
    for g in (1, 17, 65, 250):
        sum_512 = [
            tc_512[g % 8][j]
            + pools_512[0][g % 64][j]
            + pools_512[1][(g * 3 + g // 64) % 64][j]
            + pools_512[2][(g * 7 + (g // 64) * 13 + g // 4096) % 64][j]
            + pools_512[3][(g * 19 + (g // 64) * 29 + (g // 4096) * 5) % 64][j]
            for j in range(512)
        ]
        sum_128 = [
            tc_128[g % 8][j]
            + pools_128[0][g % 64][j]
            + pools_128[1][(g * 3 + g // 64) % 64][j]
            + pools_128[2][(g * 7 + (g // 64) * 13 + g // 4096) % 64][j]
            + pools_128[3][(g * 19 + (g // 64) * 29 + (g // 4096) * 5) % 64][j]
            for j in range(128)
        ]
        d_512 = _l2_normalize(sum_512)
        d_128_from_512 = _l2_normalize(d_512[:128])
        d_128_direct = _l2_normalize(sum_128)
        assert d_128_direct == pytest.approx(d_128_from_512, abs=1e-6)

    # 3. Verify _prepare_cached_vectors sorts latencies before computing p50 and p95
    s_hash = _settings(
        embedder="hashing",
        embedding_dim=512,
        embedding_storage="halfvec",
        embedding_index="none",
    )
    slug = s_hash.base_embedding_slug
    cache_dir = tmp_path / "lat_cache"
    save_npy_f32(cache_dir / f"corpus_{slug}_512.npy", [[1.0] + [0.0] * 511])
    save_npy_f32(
        cache_dir / f"queries_{slug}_512.npy",
        [[1.0] + [0.0] * 511 for _ in range(10)],
    )
    save_npy_f32(cache_dir / f"distractor_seeds_{slug}_512.npy", [[1.0] + [0.0] * 511])
    # Deliberately unsorted latencies where index 9 is small (1.0) and index 0 is huge (100.0)
    unsorted_lats = [100.0, 90.0, 80.0, 70.0, 60.0, 50.0, 40.0, 30.0, 20.0, 1.0]
    (cache_dir / f"queries_{slug}_512.meta.json").write_text(
        json.dumps(
            {
                "questions": [f"q{i}" for i in range(10)],
                "latencies_ms": unsorted_lats,
            }
        ),
        encoding="utf-8",
    )
    _, _, lat_summary, _ = await _prepare_cached_vectors(
        settings=s_hash,
        cache_dir=cache_dir,
        corpus_chunks=[],
        questions=[],
    )
    assert lat_summary["p95"] >= lat_summary["p50"]
    assert lat_summary["p95"] == pytest.approx(95.5)
    assert lat_summary["per_question_ms"]["q0"] == 100.0
    assert lat_summary["per_question_ms"]["q9"] == 1.0

    # 4. Verify _make_path_settings preserves embedding credentials and client options
    s_with_creds = _settings(
        embedder="openai_compatible",
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_api_key="secret-embed-key-123",
        embedding_api_key_file="/tmp/embed-key-file",
        embedding_model="qwen3-embedding:8b",
        embedding_model_version="64b933495768",
        embedding_dim=1024,
        embedding_native_dim=4096,
        embedding_truncate=True,
        embedding_send_dimensions=True,
        embedding_batch_size=16,
        embedding_timeout_seconds=45.0,
        embedding_max_retries=5,
        calibration_dir=str(tmp_path / "cal"),
    )
    derived = _make_path_settings(s_with_creds, embedding_index="hnsw")
    assert derived.embedding_api_key is not None
    assert derived.embedding_api_key.get_secret_value() == "secret-embed-key-123"
    assert derived.embedding_api_key_file == "/tmp/embed-key-file"
    assert derived.embedding_send_dimensions is True
    assert derived.embedding_batch_size == 16
    assert derived.embedding_timeout_seconds == 45.0
    assert derived.embedding_max_retries == 5
    assert derived.calibration_dir == str(tmp_path / "cal")

    # 5. Verify _evaluate_path computes total_search_latency_ms percentiles from paired samples
    # Anti-correlated timings: db=[10, 50, 90], q_embed=[90, 50, 10] -> paired totals=[100, 100, 100]
    # Marginal p95 sum would be 86.0 + 86.0 = 172.0, while paired p95 is 100.0
    from app.rag.retrieve import SearchBundle

    db_timings = {"q-fast-db": 10.0, "q-mid-db": 50.0, "q-slow-db": 90.0}

    async def _fake_search_knowledge(
        _session: Any,
        *,
        query: str,
        **_kwargs: Any,
    ) -> SearchBundle:
        return SearchBundle(
            hits=[],
            df={},
            n_docs=1,
            coverage=0.0,
            mode="dense",
            elapsed_ms=db_timings.get(query, 10.0),
        )

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr("evals.bench_scale.search_knowledge", _fake_search_knowledge)
        paired_questions = [
            {"id": "1", "split": "dev", "type": "fact", "answerable": False, "question": "q-fast-db", "gold": []},
            {"id": "2", "split": "dev", "type": "fact", "answerable": False, "question": "q-mid-db", "gold": []},
            {"id": "3", "split": "test", "type": "fact", "answerable": False, "question": "q-slow-db", "gold": []},
        ]
        paired_q_lat = {
            "mean": 50.0,
            "p50": 50.0,
            "p95": 86.0,
            "per_question_ms": {
                "q-fast-db": 90.0,
                "q-mid-db": 50.0,
                "q-slow-db": 10.0,
            },
        }
        path_res = await _evaluate_path(
            settings=s_hash,
            embedder=HashingEmbedder(512),
            user=Principal(id=uuid.uuid4(), role="admin"),
            questions=paired_questions,
            corpus_chunks=[],
            query_embed_latency_ms=paired_q_lat,
        )
        assert path_res["db_search_latency_ms"]["p95"] == pytest.approx(86.0)
        assert path_res["total_search_latency_ms"]["mean"] == pytest.approx(100.0)
        assert path_res["total_search_latency_ms"]["p50"] == pytest.approx(100.0)
        assert path_res["total_search_latency_ms"]["p95"] == pytest.approx(100.0)
    finally:
        mp.undo()


async def test_bench_scale_small_offline_hashing(tmp_path: Path) -> None:
    out_file = tmp_path / "bench_scale_200.json"
    cache_dir = tmp_path / "cache"
    maker = get_sessionmaker()
    seed_user_id = uuid.uuid4()
    seed_doc_id = uuid.uuid4()
    try:
        async with maker() as session:
            session.add(
                User(
                    id=seed_user_id,
                    email="seed-owner@lab.test",
                    display_name="种子维护员",
                    role="admin",
                    password_hash=hash_password("LabAgent-Demo-2026-Admin"),
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                Document(
                    id=seed_doc_id,
                    owner_id=seed_user_id,
                    title="持久种子文档",
                    collection="knowledge",
                    source="seed",
                    source_format="markdown",
                    doc_key="persistent-seed-doc",
                    visibility="lab",
                    status="pending",
                    content_text="## 种子段落\n\n清衡集群基础种子文档内容。",
                )
            )
            await session.commit()
        s_init = _settings(embedder="hashing", embedding_dim=512, embedding_storage="vector")
        await _drain_pending(HashingEmbedder(512), s_init)

        report = await run_scale_benchmark(
            chunks=200,
            embedder_kind="hashing",
            output_path=out_file,
            cache_dir=cache_dir,
            sweep_counts=[100, 200],
        )
        assert out_file.is_file()
        assert any(cache_dir.glob("*.npy"))
        assert report["suite"] == "scale"
        assert report["dataset"] == "v1"
        assert report["target_chunks"] == 200
        assert len(report["exact_scan_sweep"]) == 2
        assert [r["chunks"] for r in report["exact_scan_sweep"]] == [100, 200]
        for step in report["exact_scan_sweep"]:
            assert step["db_search_p95_ms"] >= step["db_search_p50_ms"]
        assert "exact_scan_p95_le_300ms_max_chunks" in report
        assert "known_limitations" in report and len(report["known_limitations"]) >= 1

        paths = report["paths"]
        assert set(paths.keys()) == {
            "exact_4096h",
            "two_stage_prefix1024",
            "truncated_1024_hnsw",
        }
        for path_info in paths.values():
            assert (
                path_info["db_search_latency_ms"]["p95"]
                >= path_info["db_search_latency_ms"]["p50"]
            )
        assert paths["two_stage_prefix1024"]["index_kind"] == "hnsw_prefix"
        assert paths["truncated_1024_hnsw"]["index_kind"] == "hnsw"
        assert (
            paths["two_stage_prefix1024"]["all"]["recall_at_5"]
            > paths["truncated_1024_hnsw"]["all"]["recall_at_5"]
        )
        assert report["two_stage_vs_exact"]["recall_at_5_ratio_all"] >= 0.80

        # Verify cleanup restored base index and drained pending non-eval documents
        async with maker() as session:
            assert await check_index(session, s_init) == "ok"
            restored_doc = await session.get(Document, seed_doc_id)
            assert restored_doc is not None
            assert restored_doc.status == "ready"
            chunk_cnt = int(
                (
                    await session.execute(
                        text("SELECT COUNT(*) FROM chunks WHERE document_id = :doc_id"),
                        {"doc_id": str(seed_doc_id)},
                    )
                ).scalar_one()
            )
            assert chunk_cnt >= 1
    finally:
        s_restore = _settings(embedder="hashing", embedding_dim=512, embedding_storage="vector")
        async with maker() as session:
            await apply_reindex(
                session, s_restore, HashingEmbedder(512), if_needed=False, force=True
            )
        await _drain_pending(HashingEmbedder(512), s_restore)
