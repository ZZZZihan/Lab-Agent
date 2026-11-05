"""Database tests for embedding_index_meta, reindex, storage/dimension transitions, and startup checks."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from app.acl import Principal
from app.auth import hash_password
from app.config import Settings, get_settings, reset_settings_cache
from app.db import get_sessionmaker
from app.models import User
from app.rag.embedder import HashingEmbedder, OpenAICompatibleEmbedder
from app.rag.index_meta import check_index, column_type, has_hnsw_index, read_meta
from app.rag.ingest import ingest_text
from app.rag.retrieve import search_knowledge
from app.reindex import apply_reindex
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


async def _ensure_admin_and_doc(embedder: HashingEmbedder) -> uuid.UUID:
    maker = get_sessionmaker()
    async with maker() as session:
        user = User(
            email="admin@lab.local",
            display_name="沈昭",
            role="admin",
            password_hash=hash_password("LabAgent-Demo-2026-Admin"),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        await ingest_text(
            session,
            embedder=embedder,
            owner_id=user.id,
            title="集群文档",
            text="前言说明\n\n## 硬件规模\n\n清衡-A100 集群共有 8 台节点。",
            collection="knowledge",
            visibility="lab",
            source="seed",
            doc_key="cluster-test",
        )
        await session.commit()
        return user.id


async def test_reindex_lifecycle_across_dimensions_and_storage() -> None:
    user_id = await _ensure_admin_and_doc(HashingEmbedder(512))
    maker = get_sessionmaker()

    try:
        # 1. Default hashing 512 vector -> check_index before meta is written (chunks > 0 -> ok via inferred fp)
        s_512 = _settings(embedder="hashing", embedding_dim=512, embedding_storage="vector")
        async with maker() as session:
            assert await check_index(session, s_512) == "ok"
            rebuilt = await apply_reindex(
                session, s_512, HashingEmbedder(512), if_needed=False, force=True
            )
            assert rebuilt is True
            assert await check_index(session, s_512) == "ok"
            meta = await read_meta(session)
            assert meta is not None
            assert meta.dim == 512
            assert meta.storage == "vector"
            assert meta.index_kind == "hnsw"

            # --if-needed skips when state is already ok
            skipped = await apply_reindex(
                session, s_512, HashingEmbedder(512), if_needed=True, force=False
            )
            assert skipped is False

        # 2. Switch to 4096-dim vector + index=auto -> drops HNSW, sets index_kind='none', exact scan works
        def handler_4096(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            inputs: list[str] = body["input"]
            data = []
            for idx, _ in enumerate(inputs):
                vec = [1.0, 0.5] + [0.0] * 4094
                data.append({"index": idx, "embedding": vec})
            return httpx.Response(200, json={"data": data})

        s_4096 = _settings(
            embedder="openai_compatible",
            embedding_base_url="http://127.0.0.1:11434/v1",
            embedding_model="qwen3-embedding:8b",
            embedding_model_version="64b933495768",
            embedding_dim=4096,
            embedding_native_dim=4096,
            embedding_storage="vector",
            embedding_index="auto",
        )
        client_4096 = httpx.AsyncClient(transport=httpx.MockTransport(handler_4096))
        emb_4096 = OpenAICompatibleEmbedder(s_4096, client=client_4096)

        async with maker() as session:
            # Before apply_reindex, check_index reports mismatch
            assert await check_index(session, s_4096) == "mismatch"
            rebuilt_4096 = await apply_reindex(session, s_4096, emb_4096, if_needed=True)
            assert rebuilt_4096 is True
            assert await check_index(session, s_4096) == "ok"
            assert await column_type(session) == ("vector", 4096)
            assert await has_hnsw_index(session) is False
            meta_4096 = await read_meta(session)
            assert meta_4096 is not None
            assert meta_4096.index_kind == "none"

            bundle_4096 = await search_knowledge(
                session,
                settings=s_4096,
                embedder=emb_4096,
                user=Principal(id=user_id, role="admin"),
                query="清衡-A100 节点数",
            )
            assert len(bundle_4096.hits) >= 1
            assert bundle_4096.hits[0]["cosine"] == pytest.approx(1.0, abs=1e-4)
        await client_4096.aclose()

        # 3. Switch to 1024-dim vector + index=hnsw (MRL truncate from 4096) -> creates HNSW
        s_1024 = _settings(
            embedder="openai_compatible",
            embedding_base_url="http://127.0.0.1:11434/v1",
            embedding_model="qwen3-embedding:8b",
            embedding_model_version="64b933495768",
            embedding_dim=1024,
            embedding_native_dim=4096,
            embedding_truncate=True,
            embedding_storage="vector",
            embedding_index="hnsw",
        )
        client_1024 = httpx.AsyncClient(transport=httpx.MockTransport(handler_4096))
        emb_1024 = OpenAICompatibleEmbedder(s_1024, client=client_1024)

        async with maker() as session:
            assert await check_index(session, s_1024) == "mismatch"
            await apply_reindex(session, s_1024, emb_1024)
            assert await check_index(session, s_1024) == "ok"
            assert await column_type(session) == ("vector", 1024)
            assert await has_hnsw_index(session) is True
            bundle_1024 = await search_knowledge(
                session,
                settings=s_1024,
                embedder=emb_1024,
                user=Principal(id=user_id, role="admin"),
                query="清衡-A100",
            )
            assert len(bundle_1024.hits) >= 1
        await client_1024.aclose()

        # 4. Switch to 3072-dim halfvec + index=hnsw -> creates halfvec_cosine_ops HNSW
        def handler_3072(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            inputs: list[str] = body["input"]
            data = [
                {"index": i, "embedding": [1.0, 0.25] + [0.0] * 3070}
                for i in range(len(inputs))
            ]
            return httpx.Response(200, json={"data": data})

        s_3072_half = _settings(
            embedder="openai_compatible",
            embedding_base_url="http://127.0.0.1:11434/v1",
            embedding_model="qwen3-embedding:8b",
            embedding_model_version="64b933495768",
            embedding_dim=3072,
            embedding_native_dim=3072,
            embedding_storage="halfvec",
            embedding_index="hnsw",
        )
        client_3072 = httpx.AsyncClient(transport=httpx.MockTransport(handler_3072))
        emb_3072 = OpenAICompatibleEmbedder(s_3072_half, client=client_3072)

        async with maker() as session:
            await apply_reindex(session, s_3072_half, emb_3072)
            assert await check_index(session, s_3072_half) == "ok"
            assert await column_type(session) == ("halfvec", 3072)
            assert await has_hnsw_index(session) is True
            idx_def_row = await session.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE tablename = 'chunks' AND indexname = 'chunks_embedding_hnsw'"
                )
            )
            assert "halfvec_cosine_ops" in str(idx_def_row.scalar_one())
            bundle_half = await search_knowledge(
                session,
                settings=s_3072_half,
                embedder=emb_3072,
                user=Principal(id=user_id, role="admin"),
                query="清衡-A100",
            )
            assert len(bundle_half.hits) >= 1
            assert bundle_half.hits[0]["cosine"] == pytest.approx(1.0, abs=1e-3)
        await client_3072.aclose()

        # Verify audit_events logged index_rebuilt
        async with maker() as session:
            audit_rows = list(
                await session.execute(
                    text(
                        "SELECT detail FROM audit_events WHERE action = 'index_rebuilt' ORDER BY id ASC"
                    )
                )
            )
            assert len(audit_rows) >= 4
    finally:
        # Always restore schema to default vector(512) + hnsw so subsequent tests start cleanly
        s_restore = _settings(embedder="hashing", embedding_dim=512, embedding_storage="vector")
        async with maker() as session:
            await apply_reindex(
                session, s_restore, HashingEmbedder(512), if_needed=False, force=True
            )


async def test_startup_lifespan_fails_on_index_mismatch_and_health_reports_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import app

    await _ensure_admin_and_doc(HashingEmbedder(512))
    maker = get_sessionmaker()
    s_512 = _settings()
    async with maker() as session:
        await apply_reindex(session, s_512, HashingEmbedder(512), force=True)

    # Mismatched settings: lifespan must refuse to start with clear reindex instructions
    monkeypatch.setenv("EMBEDDING_DIM", "256")
    reset_settings_cache()
    try:
        with pytest.raises(RuntimeError, match="python -m app.reindex --apply"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        monkeypatch.setenv("EMBEDDING_DIM", "512")
        reset_settings_cache()

    # Matching settings: lifespan starts and /api/v1/health exposes all new fields
    monkeypatch.delenv("EVIDENCE_SCORE_MIN", raising=False)
    monkeypatch.setenv("LOAD_TUNED_CONFIG", "true")
    reset_settings_cache()
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                resp = await http.get("/api/v1/health")
                assert resp.status_code == 200
                body = resp.json()
                assert body["embedder"] == "hashing"
                assert body["embedding_model"] == "hashing-cjk-v1"
                assert body["embedding_dim"] == 512
                assert body["embedding_storage"] == "vector"
                assert body["embedding_fingerprint"] == s_512.embedding_fingerprint
                assert body["embedding_index_ok"] is True
                assert body["embedding_base_host"] is None
                assert body["calibrated"] is True
                assert body["calibration_source"] == f"{s_512.embedding_slug}.json"
                assert "/" not in body["calibration_source"]

        # Uncalibrated settings: /api/v1/health reports calibrated=False and calibration_source=None
        monkeypatch.setenv("LOAD_TUNED_CONFIG", "false")
        reset_settings_cache()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                resp = await http.get("/api/v1/health")
                assert resp.status_code == 200
                body = resp.json()
                assert body["calibrated"] is False
                assert body["calibration_source"] is None
    finally:
        monkeypatch.setenv("EVIDENCE_SCORE_MIN", "0")
        monkeypatch.setenv("LOAD_TUNED_CONFIG", "false")
        reset_settings_cache()


async def test_run_eval_tune_writes_runtime_calibration_and_results(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals.run_eval import _run

    monkeypatch.setenv("CALIBRATION_DIR", str(tmp_path))
    reset_settings_cache()
    try:
        await _run(tune_dev=True, check=True, commit=False)
        s = get_settings()
        cal_file = tmp_path / f"{s.embedding_slug}.json"
        res_file = tmp_path / f"results-{s.embedding_slug}.json"
        assert cal_file.is_file()
        assert res_file.is_file()
        cal_data = json.loads(cal_file.read_text(encoding="utf-8"))
        assert cal_data["embedder_fingerprint"] == s.embedding_fingerprint
        assert cal_data["dataset"] == "v1"
        assert cal_data["split_used"] == "dev"
        assert "tuned_at" in cal_data
        assert "git_sha" in cal_data
        assert cal_data["evidence_score_min"] == pytest.approx(0.2475)
        res_data = json.loads(res_file.read_text(encoding="utf-8"))
        assert res_data["label"] == f"{s.embedding_slug}-v1"
        assert res_data["embedder_fingerprint"] == s.embedding_fingerprint
    finally:
        monkeypatch.delenv("CALIBRATION_DIR", raising=False)
        reset_settings_cache()
