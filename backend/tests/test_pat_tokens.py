"""Tests for Spec T4.2 / PR-29: Personal Access Tokens (la_pat_) and Streamable HTTP MCP."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.auth import hash_password, hash_pat
from app.config import get_settings, reset_settings_cache
from app.db import get_sessionmaker
from app.ingest_worker import process_next_pending
from app.mcp_server import (
    build_http_app,
    close_mcp_singletons,
    reset_mcp_singletons,
)
from app.models import ApiToken, AuditEvent, Document, User
from app.rag.embedder import build_embedder
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


async def _drain_worker() -> None:
    settings = get_settings()
    embedder = build_embedder(settings)
    try:
        while await process_next_pending(embedder, settings):
            pass
    finally:
        await embedder.aclose()


ADMIN_PW = "LabAgent-Demo-2026-Admin"
MEMBER_PW = "LabAgent-Demo-2026-Member"


async def _seed_users_and_docs() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    maker = get_sessionmaker()
    admin_id = uuid.uuid4()
    member_id = uuid.uuid4()
    lab_doc_id = uuid.uuid4()
    private_doc_id = uuid.uuid4()

    async with maker() as session:
        session.add_all(
            [
                User(
                    id=admin_id,
                    email="admin@lab.local",
                    display_name="沈昭",
                    role="admin",
                    password_hash=hash_password(ADMIN_PW),
                    is_active=True,
                ),
                User(
                    id=member_id,
                    email="member@lab.local",
                    display_name="林砚",
                    role="member",
                    password_hash=hash_password(MEMBER_PW),
                    is_active=True,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Document(
                    id=lab_doc_id,
                    owner_id=admin_id,
                    title="清衡集群使用指南",
                    collection="knowledge",
                    source="upload",
                    source_format="markdown",
                    visibility="lab",
                    status="pending",
                    content_text=(
                        "## GPU 分区\n\n"
                        "清衡实验室主力 GPU 分区为 qh-gpu，单作业最长运行 72 小时。"
                    ),
                ),
                Document(
                    id=private_doc_id,
                    owner_id=admin_id,
                    title="主任私有备忘录",
                    collection="knowledge",
                    source="upload",
                    source_format="markdown",
                    visibility="private",
                    status="pending",
                    content_text="## 绝密预算\n\n内部专项代号为曜石。",
                ),
            ]
        )
        await session.commit()

    await _drain_worker()
    return admin_id, member_id, lab_doc_id, private_doc_id


async def test_pat_lifecycle_and_scope_enforcement(client: AsyncClient) -> None:
    admin_id, member_id, lab_doc_id, _private_doc_id = await _seed_users_and_docs()

    # Login as admin to get a normal JWT session token
    login_res = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@lab.local", "password": ADMIN_PW},
    )
    assert login_res.status_code == 200
    jwt_token = login_res.json()["access_token"]
    jwt_headers = {"Authorization": f"Bearer {jwt_token}"}

    # 1. Create a PAT via POST /api/v1/tokens
    create_res = await client.post(
        "/api/v1/tokens",
        headers=jwt_headers,
        json={"name": "Workstation MCP", "expires_in_days": 30},
    )
    assert create_res.status_code == 201
    created = create_res.json()
    raw_pat = created["token"]
    token_id = uuid.UUID(created["id"])
    assert raw_pat.startswith("la_pat_")
    assert len(raw_pat) >= 50
    assert created["prefix"] == raw_pat[:15]
    assert created["name"] == "Workstation MCP"
    assert created["last_used_at"] is None
    assert created["revoked_at"] is None

    # Verify DB stores only SHA-256 hash and audit log never records raw_pat
    maker = get_sessionmaker()
    async with maker() as session:
        db_tok = await session.get(ApiToken, token_id)
        assert db_tok is not None
        assert db_tok.token_hash == hash_pat(raw_pat)
        assert raw_pat not in db_tok.token_hash

        audit_rows = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "token_create")
            )
        )
        assert len(audit_rows) == 1
        assert audit_rows[0].resource_id == str(token_id)
        assert raw_pat not in json.dumps(audit_rows[0].detail)

    # 2. List tokens via GET /api/v1/tokens (raw token must NOT be returned again)
    list_res = await client.get("/api/v1/tokens", headers=jwt_headers)
    assert list_res.status_code == 200
    listed = list_res.json()["tokens"]
    assert len(listed) == 1
    assert listed[0]["id"] == str(token_id)
    assert "token" not in listed[0]

    # 3. Allowed endpoints with PAT: /api/v1/search, GET /api/v1/documents, /api/v1/chat
    pat_headers = {"Authorization": f"Bearer {raw_pat}"}

    search_res = await client.post(
        "/api/v1/search",
        headers=pat_headers,
        json={"query": "qh-gpu", "k": 5},
    )
    assert search_res.status_code == 200
    assert len(search_res.json()["hits"]) >= 1

    docs_res = await client.get("/api/v1/documents", headers=pat_headers)
    assert docs_res.status_code == 200
    assert len(docs_res.json()["documents"]) == 2

    doc_detail_res = await client.get(
        f"/api/v1/documents/{lab_doc_id}",
        headers=pat_headers,
    )
    assert doc_detail_res.status_code == 200
    assert doc_detail_res.json()["title"] == "清衡集群使用指南"

    chat_res = await client.post(
        "/api/v1/chat",
        headers=pat_headers,
        json={"message": "清衡实验室主力 GPU 分区叫什么？"},
    )
    assert chat_res.status_code == 200
    chat_payload = chat_res.json()
    assert chat_payload["refused"] is False
    assert "answer" in chat_payload

    conv_res = await client.get("/api/v1/conversations", headers=pat_headers)
    assert conv_res.status_code == 200
    assert len(conv_res.json()["conversations"]) == 1

    # Verify last_used_at was updated on the PAT
    async with maker() as session:
        db_tok = await session.get(ApiToken, token_id)
        assert db_tok is not None
        assert db_tok.last_used_at is not None

    # 4. Forbidden endpoints with PAT must return 403 (even though user is an admin!)
    forbidden_calls = [
        ("GET", "/api/v1/admin/users", None),
        ("GET", "/api/v1/audit", None),
        ("GET", "/api/v1/tokens", None),
        ("POST", "/api/v1/tokens", {"name": "Nested PAT"}),
        ("DELETE", f"/api/v1/tokens/{token_id}", None),
        ("DELETE", f"/api/v1/documents/{lab_doc_id}", None),
        (
            "PUT",
            f"/api/v1/documents/{lab_doc_id}/acl",
            {"visibility": "lab", "user_emails": [], "group_slugs": [], "roles": []},
        ),
    ]
    for method, path, body in forbidden_calls:
        resp = await client.request(method, path, headers=pat_headers, json=body)
        assert resp.status_code == 403, f"Expected 403 for {method} {path}, got {resp.status_code}"
        assert resp.json()["error"]["code"] == "forbidden"

    upload_resp = await client.post(
        "/api/v1/documents",
        headers=pat_headers,
        data={"title": "PAT Upload", "collection": "knowledge", "visibility": "lab", "text": "## T\n\nBody"},
    )
    assert upload_resp.status_code == 403

    # 5. Another user cannot revoke admin's token (404)
    member_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "member@lab.local", "password": MEMBER_PW},
    )
    member_jwt = member_login.json()["access_token"]
    other_del = await client.delete(
        f"/api/v1/tokens/{token_id}",
        headers={"Authorization": f"Bearer {member_jwt}"},
    )
    assert other_del.status_code == 404

    # 6. Expired PAT returns 401
    async with maker() as session:
        db_tok = await session.get(ApiToken, token_id)
        assert db_tok is not None
        db_tok.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await session.commit()

    expired_res = await client.post(
        "/api/v1/search",
        headers=pat_headers,
        json={"query": "qh-gpu", "k": 5},
    )
    assert expired_res.status_code == 401

    # Restore expires_at and test explicit revocation via DELETE /api/v1/tokens/{id}
    async with maker() as session:
        db_tok = await session.get(ApiToken, token_id)
        assert db_tok is not None
        db_tok.expires_at = datetime.now(UTC) + timedelta(days=30)
        await session.commit()

    revoke_res = await client.delete(f"/api/v1/tokens/{token_id}", headers=jwt_headers)
    assert revoke_res.status_code == 200
    assert revoke_res.json()["status"] == "revoked"

    revoked_search = await client.post(
        "/api/v1/search",
        headers=pat_headers,
        json={"query": "qh-gpu", "k": 5},
    )
    assert revoked_search.status_code == 401


async def test_streamable_http_mcp_with_pat_auth(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EVIDENCE_SCORE_MIN", "0.10")
    reset_settings_cache()
    reset_mcp_singletons()

    _admin_id, _member_id, lab_doc_id, private_doc_id = await _seed_users_and_docs()

    # Issue one PAT for admin and one PAT for member
    admin_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@lab.local", "password": ADMIN_PW},
    )
    admin_jwt = admin_login.json()["access_token"]
    admin_pat_res = await client.post(
        "/api/v1/tokens",
        headers={"Authorization": f"Bearer {admin_jwt}"},
        json={"name": "Admin HTTP MCP"},
    )
    admin_pat = admin_pat_res.json()["token"]
    admin_pat_id = uuid.UUID(admin_pat_res.json()["id"])

    member_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "member@lab.local", "password": MEMBER_PW},
    )
    member_jwt = member_login.json()["access_token"]
    member_pat_res = await client.post(
        "/api/v1/tokens",
        headers={"Authorization": f"Bearer {member_jwt}"},
        json={"name": "Member HTTP MCP"},
    )
    member_pat = member_pat_res.json()["token"]

    # Ensure MCP_ACTOR_EMAIL is unset so HTTP ContextVar resolution is proven
    monkeypatch.delenv("MCP_ACTOR_EMAIL", raising=False)

    mcp_http_app = build_http_app()
    async with mcp_http_app.router.lifespan_context(mcp_http_app):
        async with httpx.AsyncClient(
            transport=ASGITransport(app=mcp_http_app),
            base_url="http://test",
        ) as mcp_client:
            # 1. Missing Authorization header -> 401
            unauth_res = await mcp_client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "list_collections", "arguments": {}},
                },
            )
            assert unauth_res.status_code == 401

            # 2. Non-PAT Bearer token (e.g. JWT) -> 401 on MCP HTTP endpoint
            jwt_on_mcp = await mcp_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {member_jwt}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "list_collections", "arguments": {}},
                },
            )
            assert jwt_on_mcp.status_code == 401

            # 3. Member PAT can search lab docs but cannot read admin's private doc
            member_search = await mcp_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {member_pat}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "search_knowledge_base",
                        "arguments": {"query": "曜石 qh-gpu", "k": 8},
                    },
                },
            )
            assert member_search.status_code == 200
            member_search_payload = json.loads(
                member_search.json()["result"]["content"][0]["text"]
            )
            member_doc_ids = {h["document_id"] for h in member_search_payload["hits"]}
            assert str(lab_doc_id) in member_doc_ids
            assert str(private_doc_id) not in member_doc_ids

            member_fetch_priv = await mcp_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {member_pat}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "fetch_document",
                        "arguments": {"document_id": str(private_doc_id)},
                    },
                },
            )
            assert member_fetch_priv.status_code == 200
            priv_text = json.loads(
                member_fetch_priv.json()["result"]["content"][0]["text"]
            )
            assert priv_text == {"status": "not_found"}

            # 4. Admin PAT on the same HTTP MCP app can read the private doc and run answer_question
            admin_fetch_priv = await mcp_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {admin_pat}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {
                        "name": "fetch_document",
                        "arguments": {"document_id": str(private_doc_id)},
                    },
                },
            )
            assert admin_fetch_priv.status_code == 200
            admin_priv_doc = json.loads(
                admin_fetch_priv.json()["result"]["content"][0]["text"]
            )
            assert admin_priv_doc["document_id"] == str(private_doc_id)
            assert "曜石" in admin_priv_doc["text"]

            admin_ans = await mcp_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {admin_pat}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 6,
                    "method": "tools/call",
                    "params": {
                        "name": "answer_question",
                        "arguments": {"question": "清衡实验室主力 GPU 分区为 qh-gpu 吗？"},
                    },
                },
            )
            assert admin_ans.status_code == 200
            ans_data = json.loads(admin_ans.json()["result"]["content"][0]["text"])
            assert ans_data["refused"] is False
            assert len(ans_data["citations"]) >= 1
            assert ans_data["citations"][0]["index"] == 1

            # 5. Revoking admin PAT causes subsequent MCP HTTP call to return 401
            await client.delete(
                f"/api/v1/tokens/{admin_pat_id}",
                headers={"Authorization": f"Bearer {admin_jwt}"},
            )
            revoked_mcp = await mcp_client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {admin_pat}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": "list_collections", "arguments": {}},
                },
            )
            assert revoked_mcp.status_code == 401

    await close_mcp_singletons()
    reset_settings_cache()
