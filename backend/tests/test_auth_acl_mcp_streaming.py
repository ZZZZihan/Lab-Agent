"""Tests for auth timing/off-loop verification, document list/get ACL visibility,
atomic Redis rate limiting, SSE stream cancellation, and MCP transport/auth hardening.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from app.auth import (
    MAX_PASSWORD_LENGTH,
    MAX_VERIFY_PASSWORD_BYTES,
    OIDC_JWKS_TIMEOUT_SECONDS,
    UNUSABLE_PASSWORD_HASH,
    _fetch_oidc_signing_key,
    _jwks_clients,
    ensure_utc,
    generate_pat,
    hash_password,
    issue_token,
    validate_password,
    verify_password,
    verify_password_async,
)
from app.config import get_settings, reset_settings_cache
from app.db import get_sessionmaker
from app.mcp_server import (
    build_http_app,
    close_mcp_singletons,
    get_mcp_limiter,
    mcp_answer_question,
    mcp_fetch_document,
    mcp_get_document_excerpt,
    mcp_list_collections,
    reset_mcp_singletons,
)
from app.models import ApiToken, AuditEvent, Document, User
from app.ratelimit import RateLimiter
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select


async def test_verify_password_async_policy_bounds_and_utc_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_hash = hash_password("ValidPass#2026Abc")
    assert await verify_password_async(real_hash, "ValidPass#2026Abc") is True
    assert await verify_password_async(real_hash, "WrongPass#2026Abc") is False

    # Missing user (None) and unusable hash still run one off-loop Argon2 check
    thread_calls: list[str] = []
    orig_to_thread = asyncio.to_thread

    async def tracked_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        thread_calls.append(getattr(func, "__name__", "callable"))
        return await orig_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", tracked_to_thread)
    assert await verify_password_async(None, "AnyAttempt#2026") is False
    assert await verify_password_async(UNUSABLE_PASSWORD_HASH, "AnyAttempt#2026") is False
    assert thread_calls == ["verify_password", "verify_password"]

    # Invalid/corrupt hash and oversized password fail closed without raising
    assert verify_password("$argon2id$corrupted-not-valid", "ValidPass#2026Abc") is False
    assert await verify_password_async("$argon2id$corrupted-not-valid", "ValidPass#2026Abc") is False
    oversized_pw = "A1!" + ("x" * (MAX_VERIFY_PASSWORD_BYTES + 10))
    assert verify_password(real_hash, oversized_pw) is False
    assert await verify_password_async(real_hash, oversized_pw) is False

    # Password length upper bound in validate_password
    with pytest.raises(ValueError, match=str(MAX_PASSWORD_LENGTH)):
        validate_password("Aa1!" + ("b" * MAX_PASSWORD_LENGTH))

    # Naive and offset-aware datetime UTC normalization
    naive_dt = datetime(2026, 11, 11, 8, 30, 0)
    assert ensure_utc(naive_dt) == datetime(2026, 11, 11, 8, 30, 0, tzinfo=UTC)
    tz_plus8 = timezone(timedelta(hours=8))
    aware_dt = datetime(2026, 11, 11, 16, 30, 0, tzinfo=tz_plus8)
    assert ensure_utc(aware_dt) == datetime(2026, 11, 11, 8, 30, 0, tzinfo=UTC)


def test_oidc_jwks_client_uses_bounded_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_kwargs: dict[str, Any] = {}

    class DummySigningKey:
        key = "dummy-public-key"

    class DummyJWKClient:
        def __init__(self, uri: str, **kwargs: Any) -> None:
            captured_kwargs["uri"] = uri
            captured_kwargs.update(kwargs)

        def get_signing_key_from_jwt(self, token: str) -> DummySigningKey:
            captured_kwargs["token"] = token
            return DummySigningKey()

    _jwks_clients.clear()
    try:
        monkeypatch.setattr("app.auth.PyJWKClient", DummyJWKClient)
        signing = _fetch_oidc_signing_key("https://idp.example.org/", "tok-123")
        assert signing.key == "dummy-public-key"
        assert captured_kwargs["uri"] == "https://idp.example.org/.well-known/jwks.json"
        assert captured_kwargs["timeout"] == OIDC_JWKS_TIMEOUT_SECONDS
        assert captured_kwargs["token"] == "tok-123"
    finally:
        _jwks_clients.clear()


async def test_rate_limiter_atomic_window_and_self_heals_missing_ttl() -> None:
    settings = get_settings().model_copy(update={"rate_limit_enabled": True})
    limiter = RateLimiter(settings)
    await limiter.close()
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter.redis = fake_redis
    try:
        bucket = "chat:atomic-test"
        assert await limiter.allow(bucket, limit=2, window_seconds=120) is True
        assert 0 < await fake_redis.ttl(bucket) <= 120
        assert await limiter.allow(bucket, limit=2, window_seconds=120) is True
        assert await limiter.allow(bucket, limit=2, window_seconds=120) is False

        # Simulate a legacy/crashed key stuck without a TTL (ttl == -1)
        stuck_bucket = "chat:stuck-no-ttl"
        await fake_redis.set(stuck_bucket, "5")
        assert await fake_redis.ttl(stuck_bucket) == -1
        assert await limiter.allow(stuck_bucket, limit=2, window_seconds=90) is False
        assert 0 < await fake_redis.ttl(stuck_bucket) <= 90
    finally:
        await limiter.close()


async def test_admin_sees_failed_and_pending_non_owner_documents_while_outsider_does_not(
    client: AsyncClient,
) -> None:
    maker = get_sessionmaker()
    admin_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    outsider_id = uuid.uuid4()
    failed_doc_id = uuid.uuid4()
    pending_doc_id = uuid.uuid4()
    eval_doc_id = uuid.uuid4()

    pw_hash = hash_password("StrongPass#2026Xy")
    async with maker() as session:
        admin_user = User(
            id=admin_id,
            email="admin-vis@lab.test",
            display_name="管理员",
            role="admin",
            password_hash=pw_hash,
            is_active=True,
        )
        owner_user = User(
            id=owner_id,
            email="owner-vis@lab.test",
            display_name="上传成员",
            role="member",
            password_hash=pw_hash,
            is_active=True,
        )
        outsider_user = User(
            id=outsider_id,
            email="outsider-vis@lab.test",
            display_name="旁观成员",
            role="member",
            password_hash=pw_hash,
            is_active=True,
        )
        session.add_all([admin_user, owner_user, outsider_user])
        await session.flush()
        session.add_all(
            [
                Document(
                    id=failed_doc_id,
                    owner_id=owner_id,
                    title="入库失败手册",
                    collection="knowledge",
                    source="upload",
                    source_format="pdf",
                    visibility="lab",
                    status="failed",
                    error_code="embedding_failed",
                    content_text="失败文档正文",
                ),
                Document(
                    id=pending_doc_id,
                    owner_id=owner_id,
                    title="待处理纪要",
                    collection="meeting",
                    source="upload",
                    source_format="markdown",
                    visibility="lab",
                    status="pending",
                    content_text="待处理文档正文",
                ),
                Document(
                    id=eval_doc_id,
                    owner_id=owner_id,
                    title="评测专用样本",
                    collection="knowledge",
                    source="eval",
                    source_format="markdown",
                    visibility="lab",
                    status="ready",
                    content_text="评测内部语料",
                ),
            ]
        )
        await session.commit()
        settings = get_settings()
        admin_jwt, _, _ = issue_token(admin_user, settings)
        owner_jwt, _, _ = issue_token(owner_user, settings)
        outsider_jwt, _, _ = issue_token(outsider_user, settings)

    admin_headers = {"Authorization": f"Bearer {admin_jwt}"}
    owner_headers = {"Authorization": f"Bearer {owner_jwt}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_jwt}"}

    # Admin sees non-owner failed and pending documents + error_code, but never eval docs
    admin_docs = (await client.get("/api/v1/documents", headers=admin_headers)).json()["documents"]
    admin_by_id = {d["id"]: d for d in admin_docs}
    assert str(failed_doc_id) in admin_by_id
    assert admin_by_id[str(failed_doc_id)]["error_code"] == "embedding_failed"
    assert str(pending_doc_id) in admin_by_id
    assert str(eval_doc_id) not in admin_by_id

    assert (
        await client.get(f"/api/v1/documents/{failed_doc_id}", headers=admin_headers)
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/documents/{pending_doc_id}", headers=admin_headers)
    ).status_code == 200
    assert (
        await client.get(f"/api/v1/documents/{eval_doc_id}", headers=admin_headers)
    ).status_code == 404

    # Owner sees their own failed and pending docs, but not eval docs on HTTP API
    owner_docs = (await client.get("/api/v1/documents", headers=owner_headers)).json()["documents"]
    owner_ids = {d["id"] for d in owner_docs}
    assert {str(failed_doc_id), str(pending_doc_id)} <= owner_ids
    assert str(eval_doc_id) not in owner_ids

    # Non-owner non-admin outsider sees neither failed nor pending docs in list or detail
    outsider_docs = (await client.get("/api/v1/documents", headers=outsider_headers)).json()[
        "documents"
    ]
    assert outsider_docs == []
    assert (
        await client.get(f"/api/v1/documents/{failed_doc_id}", headers=outsider_headers)
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/documents/{pending_doc_id}", headers=outsider_headers)
    ).status_code == 404


async def test_chat_stream_inactive_user_and_disconnect_cancels_worker(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import routes as routes_mod

    maker = get_sessionmaker()
    user_id = uuid.uuid4()
    settings = get_settings()
    async with maker() as session:
        user = User(
            id=user_id,
            email="stream-user@lab.test",
            display_name="流式测试员",
            role="member",
            password_hash=hash_password("StreamPass#2026Xy"),
            is_active=True,
        )
        session.add(user)
        await session.commit()
        jwt_tok, _, _ = issue_token(user, settings)

    # 1. Early stream close cancels in-flight run_agent task cleanly
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_run_agent(**kwargs: Any) -> dict[str, object]:
        emit = kwargs["emit"]
        await emit({"type": "status", "phase": "retrieving"})
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    monkeypatch.setattr(routes_mod, "run_agent", slow_run_agent)
    from app.auth import Actor
    from app.main import app as fastapi_app
    from starlette.requests import Request as StarletteRequest

    fake_request = StarletteRequest(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/chat/stream",
            "headers": [],
            "app": fastapi_app,
            "state": {"request_id": "req-stream-cancel"},
        }
    )
    stream_resp = await routes_mod.chat_stream(
        routes_mod.ChatBody(message="测试中途断开连接"),
        fake_request,
        Actor(id=user_id, role="member", display_name="流式测试员"),
    )
    body_iter = stream_resp.body_iterator
    first_chunk = await anext(body_iter)  # type: ignore[arg-type]
    assert "retrieving" in str(first_chunk)
    await started.wait()
    await body_iter.aclose()  # type: ignore[attr-defined]
    await asyncio.wait_for(cancelled.wait(), timeout=2.0)

    # 2. If user is deactivated right before work() checks user, stream emits unauthorized error
    async def fake_auth_local(*_args: Any, **_kwargs: Any) -> Any:
        return Actor(id=user_id, role="member", display_name="流式测试员")

    async with maker() as session:
        db_user = await session.get(User, user_id)
        assert db_user is not None
        db_user.is_active = False
        await session.commit()

    headers = {"Authorization": f"Bearer {jwt_tok}"}
    monkeypatch.setattr(routes_mod, "authenticate_local", fake_auth_local)
    deactivated_res = await client.post(
        "/api/v1/chat/stream",
        headers=headers,
        json={"message": "停用后提问"},
    )
    assert deactivated_res.status_code == 200
    assert '"code": "unauthorized"' in deactivated_res.text


async def test_mcp_password_deadline_rate_limit_dns_rebinding_and_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_mcp_singletons()
    maker = get_sessionmaker()

    active_id = uuid.uuid4()
    pending_pw_id = uuid.uuid4()
    expired_pw_id = uuid.uuid4()
    eval_doc_id = uuid.uuid4()
    pending_doc_id = uuid.uuid4()

    active_pat, active_hash, active_prefix = generate_pat()
    pending_pat, pending_hash, pending_prefix = generate_pat()
    expired_pat, expired_hash, expired_prefix = generate_pat()

    pw_hash = hash_password("McpSecure#2026Xy")
    now = datetime.now(UTC)
    async with maker() as session:
        session.add_all(
            [
                User(
                    id=active_id,
                    email="mcp-active@lab.test",
                    display_name="正常成员",
                    role="member",
                    password_hash=pw_hash,
                    is_active=True,
                ),
                User(
                    id=pending_pw_id,
                    email="mcp-pending-pw@lab.test",
                    display_name="待改密成员",
                    role="member",
                    password_hash=pw_hash,
                    is_active=True,
                    must_change_password_by=now + timedelta(days=3),
                ),
                User(
                    id=expired_pw_id,
                    email="mcp-expired-pw@lab.test",
                    display_name="口令过期成员",
                    role="member",
                    password_hash=pw_hash,
                    is_active=True,
                    must_change_password_by=now - timedelta(hours=1),
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                ApiToken(
                    user_id=active_id,
                    name="active-pat",
                    token_hash=active_hash,
                    prefix=active_prefix,
                ),
                ApiToken(
                    user_id=pending_pw_id,
                    name="pending-pat",
                    token_hash=pending_hash,
                    prefix=pending_prefix,
                ),
                ApiToken(
                    user_id=expired_pw_id,
                    name="expired-pat",
                    token_hash=expired_hash,
                    prefix=expired_prefix,
                ),
                Document(
                    id=eval_doc_id,
                    owner_id=active_id,
                    title="MCP隐藏评测文档",
                    collection="knowledge",
                    source="eval",
                    source_format="markdown",
                    visibility="lab",
                    status="ready",
                    content_text="不应通过 MCP 暴露",
                ),
                Document(
                    id=pending_doc_id,
                    owner_id=active_id,
                    title="MCP未完成入库文档",
                    collection="knowledge",
                    source="upload",
                    source_format="markdown",
                    visibility="lab",
                    status="pending",
                    content_text="未分块入库前不应暴露",
                ),
            ]
        )
        await session.commit()

    try:
        # 1. Stdio / direct MCP rejects pending or expired must_change_password_by
        monkeypatch.setenv("MCP_ACTOR_EMAIL", "mcp-pending-pw@lab.test")
        with pytest.raises(RuntimeError, match="password_change_required"):
            await mcp_list_collections()

        monkeypatch.setenv("MCP_ACTOR_EMAIL", "mcp-expired-pw@lab.test")
        with pytest.raises(RuntimeError, match="initial_password_expired"):
            await mcp_list_collections()

        # 2. MCP fetch_document / get_document_excerpt hide eval and non-ready docs
        monkeypatch.setenv("MCP_ACTOR_EMAIL", "mcp-active@lab.test")
        assert json.loads(await mcp_fetch_document(str(eval_doc_id))) == {"status": "not_found"}
        assert json.loads(await mcp_get_document_excerpt(str(eval_doc_id))) == {
            "status": "not_found"
        }
        assert json.loads(await mcp_fetch_document(str(pending_doc_id))) == {"status": "not_found"}

        # 3. MCP answer_question enforces chat rate limit and records audit status="rate_limited"
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
        monkeypatch.setenv("CHAT_RATE_LIMIT", "1")
        reset_settings_cache()
        reset_mcp_singletons()
        fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        limiter = get_mcp_limiter()
        await limiter.close()
        limiter.redis = fake_redis

        first_ans = json.loads(await mcp_answer_question("实验室有什么文档？"))
        assert first_ans["reason"] != "rate_limited"
        second_ans = json.loads(await mcp_answer_question("再次提问触发限流"))
        assert second_ans == {
            "refused": True,
            "reason": "rate_limited",
            "answer": "",
            "citations": [],
        }
        async with maker() as session:
            audit_rows = list(
                await session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.actor_id == active_id,
                        AuditEvent.resource_type == "mcp_tool",
                        AuditEvent.resource_id == "answer_question",
                    )
                )
            )
            assert any(r.detail.get("status") == "rate_limited" for r in audit_rows)

        monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
        reset_settings_cache()
        await close_mcp_singletons()

        # 4. HTTP MCP blocks pending/expired password users (403), blocks DNS rebinding Host (421),
        #    and handles concurrent lifespan-less requests safely.
        monkeypatch.delenv("MCP_ACTOR_EMAIL", raising=False)
        mcp_app = build_http_app()
        rpc_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_collections", "arguments": {}},
        }
        # Deliberately omit lifespan_context first to test concurrent fallback safety
        async with httpx.AsyncClient(
            transport=ASGITransport(app=mcp_app),
            base_url="http://test",
        ) as http:
            pending_res = await http.post(
                "/mcp",
                headers={"Authorization": f"Bearer {pending_pat}"},
                json=rpc_body,
            )
            assert pending_res.status_code == 403
            assert pending_res.json()["error"]["code"] == "password_change_required"

            expired_res = await http.post(
                "/mcp",
                headers={"Authorization": f"Bearer {expired_pat}"},
                json=rpc_body,
            )
            assert expired_res.status_code == 403
            assert expired_res.json()["error"]["code"] == "initial_password_expired"

            rebind_res = await http.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {active_pat}",
                    "Host": "evil.attacker.example",
                },
                json=rpc_body,
            )
            assert rebind_res.status_code == 421

            bad_origin_res = await http.post(
                "/mcp",
                headers={
                    "Authorization": f"Bearer {active_pat}",
                    "Origin": "http://evil.attacker.example",
                },
                json=rpc_body,
            )
            assert bad_origin_res.status_code in {400, 403}

            concurrent_responses = await asyncio.gather(
                *[
                    http.post(
                        "/mcp",
                        headers={"Authorization": f"Bearer {active_pat}"},
                        json={**rpc_body, "id": idx},
                    )
                    for idx in range(10, 16)
                ]
            )
            for res in concurrent_responses:
                assert res.status_code == 200
                payload = json.loads(res.json()["result"]["content"][0]["text"])
                assert payload["status"] == "ok"

        # 5. Lifespan shutdown closes lazy MCP singletons cleanly
        mcp_app_with_lifespan = build_http_app()
        _ = get_mcp_limiter()
        async with mcp_app_with_lifespan.router.lifespan_context(mcp_app_with_lifespan):
            pass
        from app import mcp_server as mcp_mod

        assert mcp_mod._SETTINGS is None
        assert mcp_mod._LIMITER is None
    finally:
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
        reset_settings_cache()
        await close_mcp_singletons()
