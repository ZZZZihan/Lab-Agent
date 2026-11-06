"""Comprehensive Phase 1–4 end-to-end flow and bug-fix verification tests."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
from app.admin_cli import main as admin_cli_main
from app.agent import run_agent
from app.audit import verify_audit_chain
from app.config import Settings, get_settings, repo_root, reset_settings_cache
from app.db import dispose_engine, get_sessionmaker
from app.embed_check import (
    ANCHOR_QUERY,
    DETERMINISM_QUERY,
    PARAPHRASE_DOC,
    PARAPHRASE_QUERY,
    UNRELATED_DOC,
    UNRELATED_QUERY,
    run_embed_check,
)
from app.ingest_worker import process_next_pending
from app.maintenance import run_maintenance_once
from app.mcp_server import (
    build_http_app,
    close_mcp_singletons,
    mcp_answer_question,
    mcp_fetch_document,
    mcp_get_document_excerpt,
    mcp_list_collections,
    mcp_search,
    reset_mcp_singletons,
)
from app.models import AuditEvent, Conversation, Document, Message
from app.rag.embedder import OpenAICompatibleEmbedder, build_embedder
from app.rag.rerank import RerankClient
from app.reindex import apply_reindex
from app.seed import seed
from app.smoke import parse_sse_events, run_smoke
from evals.run_e2e import _gold_map, _read_corpus, _resolve_model_settings, _run
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from tests.test_ingest import make_pdf_bytes

ADMIN_EMAIL = "admin@lab.local"
ADMIN_PW = "LabAgent-Demo-2026-Admin"
LINKE_EMAIL = "member@lab.local"
LINKE_PW = "LabAgent-Demo-2026-Member"
ZHOUNING_EMAIL = "zhou@lab.local"
ZHOUNING_PW = "LabAgent-Demo-2026-Zhou"
VIEWER_EMAIL = "viewer@lab.local"
VIEWER_PW = "LabAgent-Demo-2026-Viewer"


async def _drain_pending() -> None:
    from app.main import app

    while await process_next_pending(app.state.embedder, app.state.settings) is not None:
        pass


async def _login_jwt(client: AsyncClient, email: str, password: str) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _build_web_proxy(upstream: AsyncClient) -> FastAPI:
    web_app = FastAPI()

    def _check_origin(req: Request) -> JSONResponse | None:
        if req.method in ("GET", "HEAD"):
            return None
        if req.headers.get("origin") != "http://127.0.0.1:43123":
            return JSONResponse(
                {"error": {"code": "forbidden", "message": "跨源请求被拒绝"}},
                status_code=403,
            )
        return None

    @web_app.post("/api/session")
    async def post_session(req: Request) -> Response:
        denied = _check_origin(req)
        if denied is not None:
            return denied
        body = await req.body()
        r = await upstream.post(
            "/api/v1/auth/login",
            content=body,
            headers={"content-type": "application/json"},
        )
        if r.status_code != 200:
            return Response(
                content=r.content,
                status_code=r.status_code,
                media_type="application/json",
            )
        data = r.json()
        resp = JSONResponse({"user": data["user"]})
        resp.set_cookie("la_session", data["access_token"], httponly=True, path="/")
        return resp

    @web_app.api_route(
        "/api/backend/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def proxy_backend(path: str, req: Request) -> Response:
        denied = _check_origin(req)
        if denied is not None:
            return denied
        if req.method == "GET" and path == "documents":
            await _drain_pending()
        token = req.cookies.get("la_session")
        if not token:
            return JSONResponse({"error": {"code": "unauthorized"}}, status_code=401)
        headers = {"Authorization": f"Bearer {token}"}
        if "content-type" in req.headers:
            headers["content-type"] = req.headers["content-type"]
        body = None if req.method in ("GET", "HEAD") else await req.body()
        qs = f"?{req.url.query}" if req.url.query else ""
        r = await upstream.request(
            req.method,
            f"/api/v1/{path}{qs}",
            content=body,
            headers=headers,
        )
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )

    @web_app.get("/api/v1/health")
    async def proxy_health() -> Response:
        r = await upstream.get("/api/v1/health")
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type="application/json",
        )

    return web_app


async def test_full_phase1_to_phase4_e2e_lifecycle(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.main import app

    await seed()
    app.state.settings.evidence_score_min = 0.2475
    app.state.settings.calibration_source = "env"

    # ------------------------------------------------------------------
    # 1. Auth & CSRF & Health
    # ------------------------------------------------------------------
    health_resp = await client.get("/api/v1/health")
    assert health_resp.status_code == 200
    health_body = health_resp.json()
    assert health_body["database"] is True
    assert health_body["redis"] is True
    assert health_body["index_ok"] is True
    assert health_body["calibrated"] is True

    web_app = _build_web_proxy(client)
    async with AsyncClient(
        transport=ASGITransport(app=web_app),
        base_url="http://127.0.0.1:43123",
    ) as web_client:
        # Cross-origin POST to /api/session is blocked with 403
        csrf_login = await web_client.post(
            "/api/session",
            headers={"Origin": "https://evil.example"},
            json={"email": LINKE_EMAIL, "password": LINKE_PW},
        )
        assert csrf_login.status_code == 403
        assert csrf_login.json()["error"]["code"] == "forbidden"

        # Same-origin POST to /api/session sets HttpOnly la_session cookie
        ok_session = await web_client.post(
            "/api/session",
            headers={"Origin": "http://127.0.0.1:43123"},
            json={"email": LINKE_EMAIL, "password": LINKE_PW},
        )
        assert ok_session.status_code == 200
        assert "la_session" in web_client.cookies or "la_session" in ok_session.cookies

        # Cross-origin POST to /api/backend/documents is blocked with 403 even with cookie
        csrf_upload = await web_client.post(
            "/api/backend/documents",
            headers={"Origin": "https://evil.example"},
            data={
                "title": "CSRF 文档",
                "text": "## 小节\n内容",
                "collection": "knowledge",
                "visibility": "lab",
            },
        )
        assert csrf_upload.status_code == 403

    admin = await _login_jwt(client, ADMIN_EMAIL, ADMIN_PW)
    linke = await _login_jwt(client, LINKE_EMAIL, LINKE_PW)
    zhou = await _login_jwt(client, ZHOUNING_EMAIL, ZHOUNING_PW)
    viewer = await _login_jwt(client, VIEWER_EMAIL, VIEWER_PW)

    # ------------------------------------------------------------------
    # 2. Ingest & Multi-format Upload (.md, .txt, .pdf) + embed_check
    # ------------------------------------------------------------------
    nonce = "E2EFLOW8841"
    md_resp = await client.post(
        "/api/v1/documents",
        headers=linke,
        data={
            "title": f"超导磁体规程-{nonce}",
            "collection": "knowledge",
            "visibility": "lab",
        },
        files={
            "file": (
                "magnet-sop.md",
                f"## 励磁电流\n超导磁体批次 {nonce} 的额定励磁电流是 485 安培。".encode(),
                "text/markdown",
            )
        },
    )
    assert md_resp.status_code == 202, md_resp.text
    md_doc_id = md_resp.json()["id"]

    txt_resp = await client.post(
        "/api/v1/documents",
        headers=linke,
        data={
            "title": f"值班巡检日志-{nonce}",
            "collection": "knowledge",
            "visibility": "lab",
        },
        files={
            "file": (
                "duty-log.txt",
                f"纯文本巡检记录：冷却水回路 {nonce} 的标准回水温度为 19.5 摄氏度。".encode(),
                "text/plain",
            )
        },
    )
    assert txt_resp.status_code == 202, txt_resp.text
    txt_doc_id = txt_resp.json()["id"]

    pdf_bytes = make_pdf_bytes(
        [
            f"Page 1: Cleanroom airlock differential pressure for {nonce} is 42 Pa.",
            f"Page 2: Laser interlock shutter response limit for {nonce} is 12 ms.",
        ]
    )
    pdf_resp = await client.post(
        "/api/v1/documents",
        headers=linke,
        data={
            "title": f"洁净室安全手册-{nonce}",
            "collection": "knowledge",
            "visibility": "lab",
        },
        files={"file": ("cleanroom.pdf", pdf_bytes, "application/pdf")},
    )
    assert pdf_resp.status_code == 202, pdf_resp.text
    pdf_doc_id = pdf_resp.json()["id"]

    await _drain_pending()

    docs_list = {
        d["id"]: d
        for d in (await client.get("/api/v1/documents", headers=linke)).json()["documents"]
    }
    assert docs_list[md_doc_id]["status"] == "ready"
    assert docs_list[md_doc_id]["source_format"] == "markdown"
    assert docs_list[txt_doc_id]["status"] == "ready"
    assert docs_list[txt_doc_id]["source_format"] == "text"
    assert docs_list[pdf_doc_id]["status"] == "ready"
    assert docs_list[pdf_doc_id]["source_format"] == "pdf"

    pdf_search = await client.post(
        "/api/v1/search",
        headers=linke,
        json={"query": f"Laser interlock shutter response limit {nonce} 12 ms", "k": 5},
    )
    assert pdf_search.status_code == 200
    assert any(
        h["document_id"] == pdf_doc_id and "第 2 页" in h["heading"]
        for h in pdf_search.json()["hits"]
    )

    # Verify embed_check interleaves embed_documents between det_1 and det_2
    call_sequence: list[int] = []

    def _embed_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        call_sequence.append(len(inputs))
        data = []
        for idx, item in enumerate(inputs):
            vec = [0.0] * 64
            if ANCHOR_QUERY in item or PARAPHRASE_QUERY in item or PARAPHRASE_DOC in item:
                vec[0] = 1.0
            elif UNRELATED_QUERY in item or UNRELATED_DOC in item:
                vec[1] = 1.0
            elif DETERMINISM_QUERY in item:
                vec[2] = 1.0
            else:
                vec[3] = 1.0
            data.append({"index": idx, "embedding": vec})
        return httpx.Response(200, json={"data": data})

    ec_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=app.state.settings.database_url,
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://127.0.0.1:43123",
        embedder="openai_compatible",
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_model="lab-agent-qwen3-embed",
        embedding_model_version="64b933495768",
        embedding_dim=64,
        embedding_native_dim=64,
        load_tuned_config=False,
    )
    ec_client = httpx.AsyncClient(transport=httpx.MockTransport(_embed_handler))
    ec_embedder = OpenAICompatibleEmbedder(ec_settings, client=ec_client)
    ok_ec, line_ec = await run_embed_check(settings=ec_settings, embedder=ec_embedder)
    await ec_client.aclose()
    assert ok_ec is True, line_ec
    # 4 single-query calls (anchor, paraphrase, unrelated, det_1), then 2-doc batch, then det_2
    assert call_sequence == [1, 1, 1, 1, 2, 1]

    # ------------------------------------------------------------------
    # 3. Document ACL & Group Isolation
    # ------------------------------------------------------------------
    grp_resp = await client.post(
        "/api/v1/admin/groups",
        headers=admin,
        json={"slug": "cryo-sec", "name": "低温安全专项组"},
    )
    assert grp_resp.status_code == 200, grp_resp.text
    grp_id = grp_resp.json()["id"]
    linke_id = (await client.get("/api/v1/auth/me", headers=linke)).json()["id"]

    add_mem = await client.put(
        f"/api/v1/admin/groups/{grp_id}/members",
        headers=admin,
        json={"user_ids": [linke_id]},
    )
    assert add_mem.status_code == 200

    put_acl = await client.put(
        f"/api/v1/documents/{md_doc_id}/acl",
        headers=linke,
        json={
            "visibility": "restricted",
            "user_emails": [],
            "group_slugs": ["cryo-sec"],
            "roles": [],
        },
    )
    assert put_acl.status_code == 200, put_acl.text

    # Authorized group member (linke) can read & search; unauthorized member (zhou) gets 404 & 0 hits
    assert (await client.get(f"/api/v1/documents/{md_doc_id}", headers=linke)).status_code == 200
    assert (await client.get(f"/api/v1/documents/{md_doc_id}", headers=zhou)).status_code == 404

    zhou_search = await client.post(
        "/api/v1/search",
        headers=zhou,
        json={"query": f"超导磁体批次 {nonce} 额定励磁电流 485 安培", "k": 5},
    )
    assert zhou_search.status_code == 200
    assert all(h["document_id"] != md_doc_id for h in zhou_search.json()["hits"])

    # ------------------------------------------------------------------
    # 4. Chat SSE & Citations & Refusal
    # ------------------------------------------------------------------
    ans_resp = await client.post(
        "/api/v1/chat/stream",
        headers=linke,
        json={"message": f"超导磁体批次 {nonce} 的额定励磁电流是多少？"},
    )
    assert ans_resp.status_code == 200
    ans_events = parse_sse_events(ans_resp.text)
    cit_ev = next(e for e in ans_events if e.get("type") == "citations")
    assert any(
        isinstance(item, dict) and item.get("document_id") == md_doc_id
        for item in (cit_ev.get("items") or [])  # type: ignore[union-attr]
    )
    check_ev = next(e for e in ans_events if e.get("type") == "citation_check")
    assert check_ev.get("used") == [1]
    done_ev = next(e for e in ans_events if e.get("type") == "done")
    assert done_ev.get("refused") is False
    conv_id = str(done_ev["conversation_id"])

    ref_resp = await client.post(
        "/api/v1/chat/stream",
        headers=linke,
        json={"message": "火星基地的氧气浓度是多少？"},
    )
    assert ref_resp.status_code == 200
    ref_events = parse_sse_events(ref_resp.text)
    assert any(e.get("type") == "refusal" for e in ref_events)
    ref_done = next(e for e in ref_events if e.get("type") == "done")
    assert ref_done.get("refused") is True

    # ------------------------------------------------------------------
    # 5. Conversations CRUD
    # ------------------------------------------------------------------
    convs_resp = await client.get("/api/v1/conversations", headers=linke)
    assert convs_resp.status_code == 200
    assert any(c["id"] == conv_id for c in convs_resp.json()["conversations"])

    cont_resp = await client.post(
        "/api/v1/chat/stream",
        headers=linke,
        json={
            "message": f"冷却水回路 {nonce} 的标准回水温度是多少？",
            "conversation_id": conv_id,
        },
    )
    assert cont_resp.status_code == 200
    cont_done = next(e for e in parse_sse_events(cont_resp.text) if e.get("type") == "done")
    assert str(cont_done["conversation_id"]) == conv_id

    conv_detail = await client.get(f"/api/v1/conversations/{conv_id}", headers=linke)
    assert conv_detail.status_code == 200
    assert len(conv_detail.json()["messages"]) == 4

    assert (await client.get(f"/api/v1/conversations/{conv_id}", headers=zhou)).status_code == 404
    assert (
        await client.delete(f"/api/v1/conversations/{conv_id}", headers=zhou)
    ).status_code == 404

    del_conv = await client.delete(f"/api/v1/conversations/{conv_id}", headers=linke)
    assert del_conv.status_code == 200
    assert (await client.get(f"/api/v1/conversations/{conv_id}", headers=linke)).status_code == 404

    # ------------------------------------------------------------------
    # 6. Personal Access Tokens (la_pat_) & Scope Enforcement
    # ------------------------------------------------------------------
    pat_create = await client.post(
        "/api/v1/tokens",
        headers=linke,
        json={"name": "E2E Lifecycle PAT", "expires_in_days": 14},
    )
    assert pat_create.status_code == 201, pat_create.text
    raw_pat = pat_create.json()["token"]
    pat_id = pat_create.json()["id"]
    assert raw_pat.startswith("la_pat_")
    pat_headers = {"Authorization": f"Bearer {raw_pat}"}

    assert (await client.get("/api/v1/documents", headers=pat_headers)).status_code == 200
    assert (
        await client.post(
            "/api/v1/search",
            headers=pat_headers,
            json={"query": f"超导磁体批次 {nonce}", "k": 3},
        )
    ).status_code == 200
    pat_stream = await client.post(
        "/api/v1/chat/stream",
        headers=pat_headers,
        json={"message": f"超导磁体批次 {nonce} 的额定励磁电流是多少？"},
    )
    assert pat_stream.status_code == 200

    # PAT is rejected with 403 on admin, audit, and token-management routes
    for path in ("/api/v1/admin/users", "/api/v1/audit", "/api/v1/tokens"):
        denied_resp = await client.get(path, headers=pat_headers)
        assert denied_resp.status_code == 403

    # ------------------------------------------------------------------
    # 7. MCP Server (Direct & Streamable HTTP with PAT)
    # ------------------------------------------------------------------
    reset_mcp_singletons()
    monkeypatch.setenv("MCP_ACTOR_EMAIL", LINKE_EMAIL)
    try:
        cols_json = json.loads(await mcp_list_collections())
        assert "collections" in cols_json

        search_json = json.loads(
            await mcp_search(query=f"超导磁体批次 {nonce} 485 安培", k=5)
        )
        assert any(h["document_id"] == md_doc_id for h in search_json["hits"])

        fetched_json = json.loads(await mcp_fetch_document(document_id=md_doc_id))
        assert fetched_json["document_id"] == md_doc_id

        excerpt_json = json.loads(
            await mcp_get_document_excerpt(document_id=md_doc_id)
        )
        assert "485" in excerpt_json["excerpt"]

        ans_json = json.loads(
            await mcp_answer_question(question=f"超导磁体批次 {nonce} 的额定励磁电流是多少？")
        )
        assert ans_json["refused"] is False
        assert "485" in ans_json["answer"]
    finally:
        monkeypatch.delenv("MCP_ACTOR_EMAIL", raising=False)

    # Streamable HTTP /mcp with PAT (initialize + tools/call)
    mcp_http_app = build_http_app()
    async with mcp_http_app.router.lifespan_context(mcp_http_app):
        async with AsyncClient(
            transport=ASGITransport(app=mcp_http_app),
            base_url="http://test",
        ) as mcp_client:
            init_res = await mcp_client.post(
                "/mcp",
                headers=pat_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "e2e-test", "version": "1.0"},
                    },
                },
            )
            assert init_res.status_code == 200

            call_res = await mcp_client.post(
                "/mcp",
                headers=pat_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "search_knowledge_base",
                        "arguments": {"query": f"超导磁体批次 {nonce}", "k": 3},
                    },
                },
            )
            assert call_res.status_code == 200
            tool_payload = json.loads(call_res.json()["result"]["content"][0]["text"])
            assert any(h["document_id"] == md_doc_id for h in tool_payload["hits"])

    await close_mcp_singletons()

    # Revoke PAT and verify subsequent Bearer call returns 401
    revoke_resp = await client.delete(f"/api/v1/tokens/{pat_id}", headers=linke)
    assert revoke_resp.status_code == 200
    assert (await client.get("/api/v1/documents", headers=pat_headers)).status_code == 401

    # Verify channel="mcp" tool_call audit events are recorded
    maker = get_sessionmaker()
    async with maker() as session:
        mcp_audits = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "tool_call")
            )
        )
    assert any(ev.detail.get("channel") == "mcp" for ev in mcp_audits)

    # ------------------------------------------------------------------
    # 8. Reranker (hybrid_rrf_rerank) & Scale (hnsw_prefix)
    # ------------------------------------------------------------------
    rerank_ok_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=app.state.settings.database_url,
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://127.0.0.1:43123",
        embedder="hashing",
        embedding_dim=512,
        reranker="api",
        rerank_base_url="http://rerank.internal/v1",
        rerank_api_key="sk-rerank-e2e-test-secret",  # type: ignore[arg-type]
        rerank_model="Qwen/Qwen3-Reranker-8B",
        retrieval_mode="hybrid_rrf_rerank",
        evidence_score_min=0.5,
        cite_margin=0.20,
        cite_k=2,
        load_tuned_config=False,
    )

    def _rerank_ok_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        docs = body["documents"]
        results = [
            {"index": i, "relevance_score": 0.92 if nonce in d else 0.10}
            for i, d in enumerate(docs)
        ]
        return httpx.Response(200, json={"results": results})

    def _rerank_fail_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "reranker_down"})

    ok_rerank_client = RerankClient(
        base_url="http://rerank.internal/v1",
        model="Qwen/Qwen3-Reranker-8B",
        api_key="sk-rerank-e2e-test-secret",
        transport=httpx.MockTransport(_rerank_ok_handler),
    )
    fail_rerank_client = RerankClient(
        base_url="http://rerank.internal/v1",
        model="Qwen/Qwen3-Reranker-8B",
        api_key="sk-rerank-e2e-test-secret",
        transport=httpx.MockTransport(_rerank_fail_handler),
    )
    from app.models import User
    from app.rag.rerank import set_rerank_client

    async def _noop_emit(_ev: dict[str, object]) -> None:
        return None

    try:
        async with maker() as session:
            linke_user = await session.get(User, uuid.UUID(linke_id))
            assert linke_user is not None
            set_rerank_client(ok_rerank_client)
            out_ok = await run_agent(
                session=session,
                settings=rerank_ok_settings,
                embedder=app.state.embedder,
                llm=app.state.llm,
                user=linke_user,
                question=f"超导磁体批次 {nonce} 的额定励磁电流是多少？",
                emit=_noop_emit,
                request_id="req-rerank-ok",
                group_ids=frozenset([uuid.UUID(grp_id)]),
            )
            assert out_ok["refused"] is False
            assert len(out_ok["citations"]) >= 1  # type: ignore[arg-type]

            set_rerank_client(fail_rerank_client)
            out_fail = await run_agent(
                session=session,
                settings=rerank_ok_settings,
                embedder=app.state.embedder,
                llm=app.state.llm,
                user=linke_user,
                question=f"超导磁体批次 {nonce} 的额定励磁电流是多少？",
                emit=_noop_emit,
                request_id="req-rerank-fail",
                group_ids=frozenset([uuid.UUID(grp_id)]),
            )
            assert out_fail["refused"] is True
            assert out_fail["decision"]["reason"] == "rerank_failed"  # type: ignore[index]
    finally:
        set_rerank_client(None)

    # Verify hnsw_prefix index resolution and base fingerprint suffixing
    monkeypatch.delenv("EVIDENCE_SCORE_MIN", raising=False)
    prefix_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=app.state.settings.database_url,
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://127.0.0.1:43123",
        embedder="openai_compatible",
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_model="qwen3-embedding:8b",
        embedding_model_version="64b933495768",
        embedding_dim=4096,
        embedding_native_dim=4096,
        embedding_storage="halfvec",
        embedding_index="hnsw_prefix",
        embedding_prefix_dim=1024,
        load_tuned_config=True,
    )
    assert prefix_settings.resolved_embedding_index == "hnsw_prefix"
    assert prefix_settings.embedding_fingerprint.endswith("|ix=prefix1024")
    assert prefix_settings.calibrated is True

    # ------------------------------------------------------------------
    # 9. Audit Hash Chain & Purge Maintenance
    # ------------------------------------------------------------------
    async with maker() as session:
        exp_doc = await session.get(Document, uuid.UUID(txt_doc_id))
        assert exp_doc is not None
        exp_doc.deleted_at = datetime.now(UTC) - timedelta(
            days=app.state.settings.deleted_document_retention_days + 3
        )
        conv = Conversation(user_id=uuid.UUID(linke_id), title="保留期测试")
        session.add(conv)
        await session.flush()
        old_msg = Message(
            conversation_id=conv.id,
            role="user",
            content="过期消息内容",
            refused=False,
            citations=[],
            created_at=datetime.now(UTC)
            - timedelta(days=app.state.settings.message_retention_days + 3),
        )
        session.add(old_msg)
        await session.commit()
        old_msg_id = old_msg.id

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    maint_counts = await run_maintenance_once(app.state.settings, redis_client=fake_redis)
    await fake_redis.aclose()
    assert maint_counts is not None
    assert maint_counts["documents_purged"] >= 1
    assert maint_counts["messages_deleted"] >= 1

    async with maker() as session:
        assert await session.get(Document, uuid.UUID(txt_doc_id)) is None
        assert await session.get(Message, old_msg_id) is None
        assert await verify_audit_chain(session) is None

        all_details = list(await session.scalars(select(AuditEvent.detail)))
        serialized_audit = json.dumps(all_details, ensure_ascii=False)
        assert nonce not in serialized_audit
        assert "火星基地" not in serialized_audit
        assert raw_pat not in serialized_audit

    assert admin_cli_main(["verify-audit"]) == 0
    assert "audit chain ok" in capsys.readouterr().out
    assert (await client.get("/api/v1/audit?limit=50", headers=viewer)).status_code == 403


async def test_live_smoke_s1_to_s9_against_real_asgi_and_web_proxy(
    client: AsyncClient,
) -> None:
    from app.main import app

    await seed()
    app.state.settings.evidence_score_min = 0.2475
    app.state.settings.calibration_source = "env"
    web_app = _build_web_proxy(client)
    transport = ASGITransport(app=web_app)
    code, table = await run_smoke(
        web_url="http://127.0.0.1:43123",
        api_url="http://127.0.0.1:43124",
        offline=True,
        transport=transport,
    )
    assert code == 0, table
    for step_prefix in (
        "S1 health",
        "S2 login",
        "S3 viewer upload",
        "S4 member upload",
        "S5 cited QA",
        "S6 refusal",
        "S7 ACL isolation",
        "S8 audit trail",
        "S9 cleanup",
    ):
        assert step_prefix in table


async def test_run_e2e_v2_multiformat_acl_seeding_and_cli_model_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repo_root()
    v2_corpus_dir = root / "evals" / "data" / "v2" / "corpus"
    v2_questions_path = root / "evals" / "data" / "v2" / "questions.jsonl"

    # 1. _read_corpus and _gold_map load .md, .txt, and .pdf files from Dataset v2
    records = _read_corpus(v2_corpus_dir)
    formats = {meta.get("source_format", "markdown") for meta, _ in records}
    assert {"markdown", "text", "pdf"} <= formats
    gold_map = _gold_map(v2_corpus_dir)
    assert ("manual-cryo-cooling", "第 1 页") in gold_map
    assert ("manual-bmc-ipmi", "第 1 段") in gold_map

    # 2. _resolve_model_settings loads dataset="v2" calibration from evals/calibrations/v2/
    monkeypatch.delenv("EVIDENCE_SCORE_MIN", raising=False)
    monkeypatch.delenv("RARE_COVERAGE_MIN", raising=False)
    monkeypatch.delenv("CITE_MARGIN", raising=False)
    monkeypatch.delenv("CITE_K", raising=False)
    monkeypatch.delenv("RRF_K", raising=False)
    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    monkeypatch.setenv("LOAD_TUNED_CONFIG", "true")
    base_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=get_settings().database_url,
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://127.0.0.1:43123",
        embedder="hashing",
        embedding_dim=512,
        llm_provider="fake",
        llm_model="fake-grounded-v1",
        calibration_dir=str(tmp_path / "empty_cals"),
        load_tuned_config=True,
    )
    resolved_v2 = _resolve_model_settings(
        base_settings,
        "fake-grounded-v1",
        corpus_dir=v2_corpus_dir,
        questions_path=v2_questions_path,
        dataset="v2",
    )
    assert resolved_v2.calibrated is True
    assert resolved_v2.calibration_source is not None
    assert "v2" in Path(resolved_v2.calibration_source).parts

    # 3. _run on Dataset v2 with max_questions=10 stratifies across question types (including acl)
    #    and enforces ACL refusal (zero leakage of seed_only private/restricted docs)
    monkeypatch.setenv("EMBEDDER", "hashing")
    monkeypatch.setenv("EMBEDDING_DIM", "512")
    monkeypatch.setenv("EMBEDDING_STORAGE", "vector")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_MODEL", "fake-grounded-v1")
    reset_settings_cache()

    maker = get_sessionmaker()
    embedder = build_embedder(get_settings())
    try:
        async with maker() as session:
            rebuilt = await apply_reindex(session, get_settings(), embedder, if_needed=True)
        if rebuilt:
            while await process_next_pending(embedder, get_settings()) is not None:
                pass
    finally:
        await embedder.aclose()
        await dispose_engine()

    results = await _run(
        dataset="v2",
        split="test",
        max_questions=12,
        models=["fake-grounded-v1"],
        use_judge=False,
        commit=False,
        check=False,
    )
    assert len(results) == 1
    v2_payload = results[0]
    q_types = {q["type"] for q in v2_payload["questions"]}
    assert "acl" in q_types
    assert "unanswerable" in q_types or "unanswerable_near" in q_types
    acl_qs = [q for q in v2_payload["questions"] if q["type"] == "acl"]
    assert all(q["refused"] is True for q in acl_qs)
