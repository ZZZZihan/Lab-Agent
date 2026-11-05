"""Tests for Spec T3.6 RerankClient, hybrid_rrf_rerank mode, PII redaction, and refusal fallback."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from app.acl import Principal
from app.agent import run_agent
from app.auth import hash_password
from app.config import MODES as CONFIG_MODES
from app.config import Settings, get_settings
from app.db import get_sessionmaker
from app.llm import FakeLLM
from app.models import AuditEvent, User
from app.rag.embedder import HashingEmbedder
from app.rag.ingest import ingest_text
from app.rag.rerank import (
    RerankClient,
    RerankError,
    set_rerank_client,
)
from app.rag.retrieve import MODES as RETRIEVE_MODES
from app.rag.retrieve import search_knowledge
from evals.metrics import MODES as METRICS_MODES
from sqlalchemy import select

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


async def test_rerank_client_formats_and_outbound_pii_redaction() -> None:
    captured_requests: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/rerank"
        assert request.headers.get("Authorization") == "Bearer test-rerank-secret-key"
        body = json.loads(request.content.decode("utf-8"))
        captured_requests.append(body)
        if body.get("model") == "alt-format-model":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "score": 0.25},
                        {"index": 1, "score": 0.91},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.18},
                    {"index": 1, "relevance_score": 0.94},
                ]
            },
        )

    transport = httpx.MockTransport(_handler)
    client = RerankClient(
        base_url="http://rerank.internal:3425/v1/",
        model="bge-reranker-v2-m3",
        api_key="test-rerank-secret-key",
        timeout_seconds=5.0,
        transport=transport,
    )

    raw_query = "请联系 admin@lab.local 或拨打 13800138000 查询 A100 节点配置"
    raw_docs = [
        "普通文档段落：存储配额说明，固话 010-62781234。",
        "目标段落：联系邮箱 member@lab.local，A100 节点显存为 80GB。",
    ]
    results = await client.rerank(raw_query, raw_docs)
    assert len(results) == 2
    assert results[0]["index"] == 1
    assert results[0]["relevance_score"] == pytest.approx(0.94)
    assert results[1]["index"] == 0
    assert results[1]["relevance_score"] == pytest.approx(0.18)

    # Verify outbound PII redaction was applied before sending over HTTP
    assert len(captured_requests) == 1
    sent = captured_requests[0]
    assert sent["model"] == "bge-reranker-v2-m3"
    assert sent["top_n"] == 2
    assert "admin@lab.local" not in sent["query"]
    assert "13800138000" not in sent["query"]
    assert "[REDACTED_EMAIL]" in sent["query"]
    assert "[REDACTED_PHONE]" in sent["query"]
    assert "010-62781234" not in sent["documents"][0]
    assert "member@lab.local" not in sent["documents"][1]
    assert "[REDACTED_PHONE]" in sent["documents"][0]
    assert "[REDACTED_EMAIL]" in sent["documents"][1]

    # Verify alternative response format (`data` + `score`)
    alt_client = RerankClient(
        base_url="http://rerank.internal:3425/v1",
        model="alt-format-model",
        api_key="test-rerank-secret-key",
        transport=transport,
    )
    alt_results = await alt_client.rerank("查询显存", ["段落零", "段落一"])
    assert [item["index"] for item in alt_results] == [1, 0]
    assert alt_results[0]["relevance_score"] == pytest.approx(0.91)

    # Verify timeout and malformed response raise RerankError
    def _timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated timeout", request=request)

    timeout_client = RerankClient(
        base_url="http://rerank.internal:3425/v1",
        model="bge-reranker-v2-m3",
        transport=httpx.MockTransport(_timeout_handler),
    )
    with pytest.raises(RerankError) as exc_info:
        await timeout_client.rerank("q", ["d1"])
    assert exc_info.value.code == "rerank_timeout"

    def _bad_payload_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"index": 99, "relevance_score": 0.5}]})

    bad_client = RerankClient(
        base_url="http://rerank.internal:3425/v1",
        model="bge-reranker-v2-m3",
        transport=httpx.MockTransport(_bad_payload_handler),
    )
    with pytest.raises(RerankError):
        await bad_client.rerank("q", ["d1"])


def test_reranker_settings_and_fingerprint_stability() -> None:
    assert "hybrid_rrf_rerank" in CONFIG_MODES
    assert "hybrid_rrf_rerank" in RETRIEVE_MODES
    assert "hybrid_rrf_rerank" in METRICS_MODES

    s_off = _settings(reranker="off")
    assert (
        s_off.embedding_fingerprint
        == "hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442"
    )
    assert "|rr=" not in s_off.embedding_fingerprint

    s_api = _settings(
        reranker="api",
        llm_base_url="http://127.0.0.1:3425/v1",
        llm_api_key="sk-test-llm-key-value-1234",
        rerank_model="bge-reranker-v2-m3",
        rerank_model_version="202611",
    )
    assert s_api.rerank_base_url == "http://127.0.0.1:3425/v1"
    assert s_api.rerank_api_key is not None
    assert s_api.rerank_api_key.get_secret_value() == "sk-test-llm-key-value-1234"
    assert s_api.embedding_fingerprint.endswith("|rr=bge-reranker-v2-m3@202611")
    assert "rr-bge-reranker-v2-m3-202611" in s_api.embedding_slug

    s_api_default_ver = _settings(
        reranker="api",
        rerank_base_url="http://127.0.0.1:8090/v1",
        rerank_model="Qwen3-Reranker-4B",
    )
    assert s_api_default_ver.embedding_fingerprint.endswith("|rr=Qwen3-Reranker-4B@v1")


async def test_search_knowledge_and_agent_rerank_success_and_failure_fallback() -> None:
    maker = get_sessionmaker()
    user_id = uuid.uuid4()
    embedder = HashingEmbedder(512)
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
        doc_a = await ingest_text(
            session,
            embedder=embedder,
            owner_id=user.id,
            title="文档甲：常规网关说明",
            text="## 网关概览\n\n清衡实验室内部网关默认端口说明与基础排障指南。",
            collection="knowledge",
            visibility="lab",
            source="seed",
            doc_key="rerank-doc-a",
            storage="vector",
        )
        doc_b = await ingest_text(
            session,
            embedder=embedder,
            owner_id=user.id,
            title="文档乙：重排优先手册",
            text="## 核心结论\n\n清衡实验室量子低温柜的巡检周期严格设定为每 14 天一次。",
            collection="knowledge",
            visibility="lab",
            source="seed",
            doc_key="rerank-doc-b",
            storage="vector",
        )
        await session.commit()
        doc_b_id = str(doc_b.id)

    # 1. Success path: mock reranker promotes doc_b to the top with high rerank_score
    def _success_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        docs = body["documents"]
        results = []
        for idx, text_item in enumerate(docs):
            score = 0.96 if "14 天" in text_item else 0.12
            results.append({"index": idx, "relevance_score": score})
        return httpx.Response(200, json={"results": results})

    ok_client = RerankClient(
        base_url="http://rerank.internal:3425/v1",
        model="bge-reranker-v2-m3",
        transport=httpx.MockTransport(_success_handler),
    )
    s_rerank = _settings(
        reranker="api",
        rerank_base_url="http://rerank.internal:3425/v1",
        rerank_model="bge-reranker-v2-m3",
        retrieval_mode="hybrid_rrf_rerank",
        evidence_score_min=0.50,
        rare_coverage_min=0.0,
        cite_margin=0.15,
        cite_k=2,
    )

    try:
        set_rerank_client(ok_client)
        async with maker() as session:
            bundle = await search_knowledge(
                session,
                settings=s_rerank,
                embedder=embedder,
                user=Principal(id=user_id, role="admin"),
                query="清衡实验室量子低温柜的巡检周期是多久？",
            )
            assert bundle.rerank_failed is False
            assert bundle.mode == "hybrid_rrf_rerank"
            assert len(bundle.hits) >= 2
            assert bundle.hits[0]["document_id"] == doc_b_id
            assert bundle.hits[0]["rerank_score"] == pytest.approx(0.96)

            events_ok: list[dict[str, object]] = []

            async def _emit_ok(ev: dict[str, object]) -> None:
                events_ok.append(ev)

            db_user = await session.get(User, user_id)
            assert db_user is not None
            agent_ok = await run_agent(
                session=session,
                user=db_user,
                settings=s_rerank,
                embedder=embedder,
                llm=FakeLLM(s_rerank),
                question="清衡实验室量子低温柜的巡检周期是多久？",
                emit=_emit_ok,
                request_id="req-rerank-ok",
            )
            assert agent_ok["refused"] is False
            assert "14 天" in str(agent_ok["answer"])

        # 2. Failure path: upstream reranker returns 500 -> fallback to hybrid_rrf,
        # write `rerank_failed` audit event, and `run_agent` refuses with `reason="rerank_failed"`
        def _fail_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "upstream overloaded"})

        fail_client = RerankClient(
            base_url="http://rerank.internal:3425/v1",
            model="bge-reranker-v2-m3",
            transport=httpx.MockTransport(_fail_handler),
        )
        set_rerank_client(fail_client)

        async with maker() as session:
            baseline_rrf = await search_knowledge(
                session,
                settings=_settings(retrieval_mode="hybrid_rrf"),
                embedder=embedder,
                user=Principal(id=user_id, role="admin"),
                query="清衡实验室量子低温柜的巡检周期是多久？",
                mode="hybrid_rrf",
            )
            fallback_bundle = await search_knowledge(
                session,
                settings=s_rerank,
                embedder=embedder,
                user=Principal(id=user_id, role="admin"),
                query="清衡实验室量子低温柜的巡检周期是多久？",
                request_id="req-rerank-search-fail",
            )
            assert fallback_bundle.rerank_failed is True
            assert [h["chunk_id"] for h in fallback_bundle.hits] == [
                h["chunk_id"] for h in baseline_rrf.hits
            ]

            events_fail: list[dict[str, object]] = []

            async def _emit_fail(ev: dict[str, object]) -> None:
                events_fail.append(ev)

            db_user = await session.get(User, user_id)
            assert db_user is not None
            agent_fail = await run_agent(
                session=session,
                user=db_user,
                settings=s_rerank,
                embedder=embedder,
                llm=FakeLLM(s_rerank),
                question="清衡实验室量子低温柜的巡检周期是多久？",
                emit=_emit_fail,
                request_id="req-rerank-agent-fail",
            )
            assert agent_fail["refused"] is True
            assert agent_fail["reason"] == "rerank_failed"
            decision = agent_fail["decision"]
            assert isinstance(decision, dict)
            assert decision["reason"] == "rerank_failed"
            assert any(
                ev.get("type") == "refusal" and ev.get("reason") == "rerank_failed"
                for ev in events_fail
            )

        async with maker() as session:
            rerank_audits = list(
                await session.scalars(
                    select(AuditEvent).where(AuditEvent.action == "rerank_failed")
                )
            )
            assert len(rerank_audits) >= 2
            assert rerank_audits[-1].resource_type == "reranker"
            assert rerank_audits[-1].detail.get("model") == "bge-reranker-v2-m3"
            assert rerank_audits[-1].detail.get("reason") == "upstream_error"

            refusal_audits = list(
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "answer_refused",
                        AuditEvent.request_id == "req-rerank-agent-fail",
                    )
                )
            )
            assert len(refusal_audits) == 1
            assert refusal_audits[0].detail.get("reason") == "rerank_failed"
    finally:
        set_rerank_client(None)
        del doc_a
