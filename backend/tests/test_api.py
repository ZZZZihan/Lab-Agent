"""API checks for login, ACL at retrieval time, grounded refusal, and audit."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from app.auth import hash_password
from app.db import get_sessionmaker
from app.llm import CAPTURED
from app.models import User
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

PASSWORD = "LabAgent-Test-Password-2026"


async def _add_user(email: str, role: str, name: str) -> None:
    maker = get_sessionmaker()
    async with maker() as session:
        session.add(
            User(
                email=email,
                display_name=name,
                role=role,
                password_hash=hash_password(PASSWORD),
                is_active=True,
            )
        )
        await session.commit()


async def _login(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _upload(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    title: str,
    body: str,
    visibility: str = "lab",
    collection: str = "knowledge",
    grant_emails: str | None = None,
    process: bool = True,
) -> str:
    from app.ingest_worker import process_next_pending
    from app.main import app

    form = {
        "title": title,
        "text": body,
        "visibility": visibility,
        "collection": collection,
    }
    if grant_emails:
        form["grant_emails"] = grant_emails
    response = await client.post("/api/v1/documents", data=form, headers=headers)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "pending"
    if process:
        while await process_next_pending(app.state.embedder, app.state.settings) is not None:
            pass
    return str(payload["id"])


def _events(raw: str) -> list[dict[str, object]]:
    events = []
    for block in raw.split("\n\n"):
        if "data:" not in block:
            continue
        data = block.split("data:", 1)[1].strip()
        events.append(json.loads(data))
    return events


@pytest.fixture
async def people(client: AsyncClient) -> AsyncIterator[dict[str, dict[str, str]]]:
    del client
    await _add_user("admin@lab.local", "admin", "沈昭")
    await _add_user("member@lab.local", "member", "林可")
    await _add_user("zhou@lab.local", "member", "周宁")
    await _add_user("viewer@lab.local", "viewer", "王敏")
    yield {}


async def test_login_and_viewer_cannot_upload(client: AsyncClient, people: dict[str, dict[str, str]]) -> None:
    del people
    headers = await _login(client, "viewer@lab.local")
    response = await client.post(
        "/api/v1/documents",
        data={"title": "不行", "text": "正文\n\n## 一\n\n事实。", "visibility": "lab", "collection": "knowledge"},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.json()["role"] == "viewer"


async def test_private_chunk_is_invisible_at_retrieval(client: AsyncClient, people: dict[str, dict[str, str]]) -> None:
    del people
    admin = await _login(client, "admin@lab.local")
    member = await _login(client, "member@lab.local")
    sentinel = "仅导师可见批注-HX17-负例不足"
    document_id = await _upload(
        client,
        admin,
        title="私下批注",
        body=f"概述\n\n## 批注\n\n{sentinel}。进度正常。",
        visibility="private",
    )
    hidden = await client.post("/api/v1/search", json={"query": sentinel, "k": 8}, headers=member)
    assert hidden.status_code == 200
    blob = hidden.text
    assert sentinel not in blob
    assert hidden.json()["hits"] == []
    visible = await client.post("/api/v1/search", json={"query": sentinel, "k": 8}, headers=admin)
    assert sentinel in visible.text
    denied = await client.get(f"/api/v1/documents/{document_id}", headers=member)
    assert denied.status_code == 404


async def test_restricted_grant_and_chat_citation(client: AsyncClient, people: dict[str, dict[str, str]]) -> None:
    del people
    admin = await _login(client, "admin@lab.local")
    member = await _login(client, "member@lab.local")
    outsider = await _login(client, "zhou@lab.local")
    fact = "赫利俄斯内部代号是 HX-17，负责人是陈予安。"
    await _upload(
        client,
        admin,
        title="项目赫利俄斯",
        body=f"项目说明\n\n## 目标\n\n{fact}",
        visibility="restricted",
        collection="project",
        grant_emails="member@lab.local",
    )
    outsider_hits = await client.post(
        "/api/v1/search", json={"query": "HX-17 负责人", "k": 5}, headers=outsider
    )
    assert "HX-17" not in outsider_hits.text
    response = await client.post("/api/v1/chat/stream", json={"message": "赫利俄斯的内部代号是什么？"}, headers=member)
    assert response.status_code == 200
    events = _events(response.text)
    kinds = [event["type"] for event in events]
    assert "citations" in kinds
    assert "token" in kinds
    assert "done" in kinds
    citations = next(event for event in events if event["type"] == "citations")
    assert any("HX-17" in str(item.get("excerpt")) for item in citations["items"])  # type: ignore[union-attr]
    done = next(event for event in events if event["type"] == "done")
    assert done["refused"] is False


async def test_weak_evidence_refuses_without_inventing(client: AsyncClient, people: dict[str, dict[str, str]]) -> None:
    del people
    from app.main import app

    member = await _login(client, "member@lab.local")
    await _upload(
        client,
        member,
        title="集群",
        body="集群说明\n\n## 硬件规模\n\n清衡-A100 共有 8 台节点。",
        visibility="lab",
    )
    app.state.settings.evidence_score_min = 0.99
    try:
        response = await client.post(
            "/api/v1/chat/stream",
            json={"message": "清衡-A100 一共有多少台节点？"},
            headers=member,
        )
    finally:
        app.state.settings.evidence_score_min = 0.0
    events = _events(response.text)
    refusal = next(event for event in events if event["type"] == "refusal")
    assert refusal["reason"] in {"low_score", "low_coverage", "no_hits"}
    assert not any(event["type"] == "token" for event in events)
    assert CAPTURED == []


async def test_llm_capture_is_redacted_and_audit_has_no_prompt(client: AsyncClient, people: dict[str, dict[str, str]]) -> None:
    del people
    member = await _login(client, "member@lab.local")
    await _upload(
        client,
        member,
        title="联系",
        body="说明\n\n## 上报\n\n事故请写信给 sec@qingheng.lab，电话 13812345678。",
        visibility="lab",
    )
    response = await client.post(
        "/api/v1/chat/stream",
        json={"message": "安全事故要发给哪个地址？"},
        headers=member,
    )
    assert response.status_code == 200
    assert CAPTURED, "fake model should see the redacted prompt in test mode"
    outbound = "\n".join(CAPTURED)
    assert "sec@qingheng.lab" not in outbound
    assert "13812345678" not in outbound
    assert "[REDACTED_EMAIL]" in outbound
    maker = get_sessionmaker()
    async with maker() as session:
        rows = await session.execute(text("SELECT action, detail::text FROM audit_events"))
        blob = " ".join(f"{action} {detail}" for action, detail in rows)
    assert "sec@qingheng.lab" not in blob
    assert "安全事故要发给哪个地址" not in blob
    assert "llm_call" in blob
    assert "prompt_tokens" in blob


async def test_audit_is_append_only(client: AsyncClient) -> None:
    del client
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(
            text(
                "INSERT INTO audit_events (action, detail) VALUES ('login', '{\"method\": \"password\"}'::jsonb)"
            )
        )
        await session.commit()
    async with maker() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text("UPDATE audit_events SET action = 'tampered'"))
            await session.commit()
    async with maker() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text("DELETE FROM audit_events"))
            await session.commit()
    async with maker() as session:
        await session.execute(text("SELECT set_config('labagent.audit_retention', 'on', true)"))
        await session.execute(text("DELETE FROM audit_events"))
        await session.commit()
    async with maker() as session:
        remaining = await session.scalar(text("SELECT count(*) FROM audit_events"))
        assert not remaining


async def test_eval_account_is_locked_and_cannot_login(client: AsyncClient) -> None:
    from app.auth import UNUSABLE_PASSWORD_HASH
    from app.config import get_settings
    from evals.run_eval import _prepare
    from sqlalchemy import select

    maker = get_sessionmaker()
    async with maker() as session:
        session.add(
            User(
                email="eval@lab.local",
                display_name="评测",
                role="admin",
                password_hash=hash_password(PASSWORD),
                is_active=True,
            )
        )
        await session.commit()

    await _prepare(get_settings())

    async with maker() as session:
        eval_user = await session.scalar(select(User).where(User.email == "eval@lab.local"))
        assert eval_user is not None
        assert eval_user.password_hash == UNUSABLE_PASSWORD_HASH
        assert eval_user.is_active is False

    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "eval@lab.local", "password": PASSWORD},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_openai_compatible_citation_filtering_and_audit(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    import httpx
    from app.config import Settings
    from app.llm import OpenAICompatibleLLM
    from app.main import app
    from app.models import Message
    from sqlalchemy import select

    member = await _login(client, "member@lab.local")
    await _upload(
        client,
        member,
        title="集群规范",
        body="前言说明\n\n## 硬件规模\n\n清衡-A100 共有 8 台节点。\n\n## 预约上限\n\n单次预约最长 24 小时。",
        visibility="lab",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        sse = (
            'data: {"choices": [{"delta": {"content": "清衡-A100 共有 8 台节点 [1]，无效编号 [9]。"}}]}\n\n'
            'data: {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 12}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=sse.encode())

    llm_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=app.state.settings.database_url,
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://127.0.0.1:43123",
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:3425/v1",
        llm_api_key="ci-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        llm_model="gemini-3.8-flash",
        load_tuned_config=False,
    )
    original_llm = app.state.llm
    mock_llm = OpenAICompatibleLLM(llm_settings, transport=httpx.MockTransport(handler))
    app.state.llm = mock_llm
    try:
        question = "清衡-A100 节点数量与预约上限是什么？"
        response = await client.post(
            "/api/v1/chat/stream",
            json={"message": question},
            headers=member,
        )
    finally:
        app.state.llm = original_llm
        await mock_llm.aclose()

    assert response.status_code == 200
    events = _events(response.text)
    initial_citations = next(e for e in events if e["type"] == "citations")
    assert len(initial_citations["items"]) >= 2  # type: ignore[arg-type]
    check_ev = next(e for e in events if e["type"] == "citation_check")
    assert check_ev["used"] == [1]
    assert check_ev["invalid"] == [9]
    assert check_ev["uncited"] is False

    maker = get_sessionmaker()
    async with maker() as session:
        assistant_msg = await session.scalar(
            select(Message).where(Message.role == "assistant")
        )
        assert assistant_msg is not None
        assert len(assistant_msg.citations) == 1
        assert assistant_msg.citations[0]["index"] == 1
        audit_row = await session.execute(
            text("SELECT detail FROM audit_events WHERE action = 'llm_call' ORDER BY id DESC LIMIT 1")
        )
        detail = audit_row.scalar_one()
        assert detail["status"] == "ok"
        assert detail["citations_used"] == [1]
        assert detail["citations_invalid"] == [9]
        assert detail["uncited"] is False
        assert detail["declined"] is False
        assert question not in json.dumps(detail, ensure_ascii=False)


async def test_upstream_500_emits_llm_unavailable_and_persists_message(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    import httpx
    from app.config import Settings
    from app.llm import OpenAICompatibleLLM
    from app.main import app
    from app.models import Message
    from sqlalchemy import select

    member = await _login(client, "member@lab.local")
    await _upload(
        client,
        member,
        title="集群规范",
        body="前言\n\n## 硬件规模\n\n清衡-A100 共有 8 台节点。",
        visibility="lab",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"error":"upstream failure"}')

    llm_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=app.state.settings.database_url,
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://127.0.0.1:43123",
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:3425/v1",
        llm_api_key="ci-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        llm_model="gemini-3.8-flash",
        llm_max_retries=1,
        load_tuned_config=False,
    )
    original_llm = app.state.llm
    mock_llm = OpenAICompatibleLLM(llm_settings, transport=httpx.MockTransport(handler))
    app.state.llm = mock_llm
    try:
        response = await client.post(
            "/api/v1/chat/stream",
            json={"message": "清衡-A100 一共有多少台节点？"},
            headers=member,
        )
    finally:
        app.state.llm = original_llm
        await mock_llm.aclose()

    assert response.status_code == 200
    events = _events(response.text)
    assert any(e["type"] == "error" and e.get("code") == "llm_unavailable" for e in events)
    last_event = events[-1]
    assert last_event["type"] == "done"
    assert last_event.get("error") == "llm_unavailable"

    maker = get_sessionmaker()
    async with maker() as session:
        assistant_msg = await session.scalar(
            select(Message).where(Message.role == "assistant")
        )
        assert assistant_msg is not None
        assert assistant_msg.content == "模型服务暂时不可用，没有生成回答。请稍后重试。"
        assert assistant_msg.refused is False
        assert assistant_msg.citations == []


async def test_model_declined_converts_to_refusal_and_audits(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    import httpx
    from app.config import Settings
    from app.llm import MODEL_DECLINE, OpenAICompatibleLLM
    from app.main import app
    from app.models import Message
    from sqlalchemy import select

    member = await _login(client, "member@lab.local")
    await _upload(
        client,
        member,
        title="集群规范",
        body="前言\n\n## 硬件规模\n\n清衡-A100 共有 8 台节点。",
        visibility="lab",
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        sse = (
            f'data: {{"choices": [{{"delta": {{"content": "{MODEL_DECLINE}。"}}}}]}}\n\n'
            'data: {"choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 8}}\n\n'
            "data: [DONE]\n\n"
        )
        return httpx.Response(200, content=sse.encode())

    llm_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url=app.state.settings.database_url,
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://127.0.0.1:43123",
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:3425/v1",
        llm_api_key="ci-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        llm_model="gemini-3.8-flash",
        load_tuned_config=False,
    )
    original_llm = app.state.llm
    mock_llm = OpenAICompatibleLLM(llm_settings, transport=httpx.MockTransport(handler))
    app.state.llm = mock_llm
    try:
        response = await client.post(
            "/api/v1/chat/stream",
            json={"message": "清衡-A100 的电源型号是什么？"},
            headers=member,
        )
    finally:
        app.state.llm = original_llm
        await mock_llm.aclose()

    assert response.status_code == 200
    events = _events(response.text)
    refusal_ev = next(e for e in events if e["type"] == "refusal")
    assert refusal_ev["reason"] == "model_declined"
    done_ev = next(e for e in events if e["type"] == "done")
    assert done_ev["refused"] is True

    maker = get_sessionmaker()
    async with maker() as session:
        assistant_msg = await session.scalar(
            select(Message).where(Message.role == "assistant")
        )
        assert assistant_msg is not None
        assert assistant_msg.refused is True
        assert assistant_msg.content == "模型认为现有证据不足以回答这个问题。"
        audit_row = await session.execute(
            text(
                "SELECT detail FROM audit_events WHERE action = 'answer_refused' ORDER BY id DESC LIMIT 1"
            )
        )
        detail = audit_row.scalar_one()
        assert detail["reason"] == "model_declined"


async def test_sentinel_api_key_never_leaks_on_401_or_500(
    client: AsyncClient,
    people: dict[str, dict[str, str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    del people
    import httpx
    from app.config import Settings
    from app.llm import OpenAICompatibleLLM
    from app.main import app

    sentinel = "sk-sentinel-not-real"
    member = await _login(client, "member@lab.local")
    await _upload(
        client,
        member,
        title="集群规范",
        body="前言\n\n## 硬件规模\n\n清衡-A100 共有 8 台节点。",
        visibility="lab",
    )

    for status_code in (401, 500):

        def handler(_request: httpx.Request, code: int = status_code) -> httpx.Response:
            return httpx.Response(
                code,
                content=f'{{"error":"invalid key {sentinel}"}}'.encode(),
            )

        llm_settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            app_env="test",
            database_url=app.state.settings.database_url,
            jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
            cors_origins="http://127.0.0.1:43123",
            llm_provider="openai_compatible",
            llm_base_url=f"http://127.0.0.1:3425/v1?secret={sentinel}",
            llm_api_key=sentinel,  # type: ignore[arg-type]
            llm_model="gemini-3.8-flash",
            llm_max_retries=0,
            load_tuned_config=False,
        )
        original_llm = app.state.llm
        original_settings = app.state.settings
        mock_llm = OpenAICompatibleLLM(llm_settings, transport=httpx.MockTransport(handler))
        app.state.llm = mock_llm
        app.state.settings.llm_base_url = llm_settings.llm_base_url
        app.state.settings.llm_api_key = llm_settings.llm_api_key
        try:
            chat_resp = await client.post(
                "/api/v1/chat/stream",
                json={"message": "清衡-A100 一共有多少台节点？"},
                headers=member,
            )
            health_resp = await client.get("/api/v1/health")
            assert sentinel not in chat_resp.text
            assert sentinel not in health_resp.text
            assert health_resp.json().get("llm_base_host") == "127.0.0.1:3425"
        finally:
            app.state.llm = original_llm
            app.state.settings = original_settings
            await mock_llm.aclose()

    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err

    maker = get_sessionmaker()
    async with maker() as session:
        audit_rows = await session.execute(text("SELECT action, detail::text FROM audit_events"))
        audit_blob = " ".join(f"{a} {d}" for a, d in audit_rows)
        msg_rows = await session.execute(text("SELECT content FROM messages"))
        msg_blob = " ".join(str(r[0]) for r in msg_rows)
    assert sentinel not in audit_blob
    assert sentinel not in msg_blob


async def test_llm_redact_false_sends_raw_text_and_audits_flag(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    from app.main import app

    member = await _login(client, "member@lab.local")
    await _upload(
        client,
        member,
        title="联系",
        body="说明\n\n## 上报\n\n事故请写信给 sec@qingheng.lab，电话 13812345678。",
        visibility="lab",
    )
    app.state.settings.llm_redact = False
    try:
        response = await client.post(
            "/api/v1/chat/stream",
            json={"message": "安全事故要发给哪个地址？"},
            headers=member,
        )
    finally:
        app.state.settings.llm_redact = True
    assert response.status_code == 200
    outbound = "\n".join(CAPTURED)
    assert "sec@qingheng.lab" in outbound
    assert "[REDACTED_EMAIL]" not in outbound

    maker = get_sessionmaker()
    async with maker() as session:
        audit_row = await session.execute(
            text("SELECT detail FROM audit_events WHERE action = 'llm_call' ORDER BY id DESC LIMIT 1")
        )
        detail = audit_row.scalar_one()
        assert detail["redaction_count"] == 0
        assert detail["redact"] is False


async def test_embedding_unavailable_emits_error_without_refusal(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    from app.agent import EmbeddingError
    from app.main import app

    member = await _login(client, "member@lab.local")

    class BrokenEmbedder:
        name = "broken"
        dim = 512

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingError("conn_refused")

        def embed_query(self, text: str) -> list[float]:
            raise EmbeddingError("conn_refused")

    original_embedder = app.state.embedder
    app.state.embedder = BrokenEmbedder()
    try:
        response = await client.post(
            "/api/v1/chat/stream",
            json={"message": "清衡-A100 一共有多少台节点？"},
            headers=member,
        )
    finally:
        app.state.embedder = original_embedder

    assert response.status_code == 200
    events = _events(response.text)
    assert any(e["type"] == "error" and e.get("code") == "embedding_unavailable" for e in events)
    assert not any(e["type"] == "refusal" for e in events)
    assert events[-1]["type"] == "done"
    assert events[-1].get("error") == "embedding_unavailable"

    maker = get_sessionmaker()
    async with maker() as session:
        audit_row = await session.execute(
            text("SELECT detail FROM audit_events WHERE action = 'retrieval' ORDER BY id DESC LIMIT 1")
        )
        detail = audit_row.scalar_one()
        assert detail["status"] == "error"
        assert detail["error"] == "conn_refused"


async def test_project_question_retrieves_from_knowledge_collection(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    member = await _login(client, "member@lab.local")
    doc_id = await _upload(
        client,
        member,
        title="星火笔记",
        body="## 项目代号\n星火项目的内部代号是 SF-42。",
        visibility="lab",
        collection="knowledge",
    )
    response = await client.post(
        "/api/v1/chat/stream",
        json={"message": "星火项目的内部代号是什么？"},
        headers=member,
    )
    assert response.status_code == 200
    events = _events(response.text)
    tool_names = [str(event.get("name")) for event in events if event["type"] == "tool"]
    assert tool_names == ["search_knowledge_base"]
    done = next(event for event in events if event["type"] == "done")
    assert done["refused"] is False
    tokens = "".join(str(event.get("text") or "") for event in events if event["type"] == "token")
    assert "SF-42" in tokens
    citations = next(event for event in events if event["type"] == "citations")
    items = citations["items"]
    assert isinstance(items, list) and len(items) >= 1
    assert any(
        item.get("document_id") == doc_id and "SF-42" in str(item.get("excerpt"))
        for item in items
        if isinstance(item, dict)
    )


async def test_client_ip_forwarding_and_login_rate_limit_audit(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    import fakeredis.aioredis
    from app.audit import hash_ip
    from app.main import app
    from pydantic import SecretStr

    proxy_secret = "ci-only-jwt-secret-value-0123456789-abcdef"
    jwt_secret_val = app.state.settings.jwt_secret.get_secret_value()
    original_proxy_secret = app.state.settings.internal_proxy_secret
    original_rate_limit = app.state.settings.rate_limit_enabled
    original_redis = app.state.limiter.redis
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    app.state.settings.internal_proxy_secret = SecretStr(proxy_secret)
    try:
        # 1. Valid X-Internal-Proxy-Secret + valid X-Client-IP -> ip_hash matches 203.0.113.7
        resp_ok = await client.post(
            "/api/v1/auth/login",
            json={"email": "member@lab.local", "password": PASSWORD},
            headers={
                "X-Internal-Proxy-Secret": proxy_secret,
                "X-Client-IP": "203.0.113.7",
            },
        )
        assert resp_ok.status_code == 200
        expected_forwarded_hash = hash_ip("203.0.113.7", jwt_secret_val)

        maker = get_sessionmaker()
        async with maker() as session:
            row = await session.execute(
                text("SELECT ip_hash FROM audit_events WHERE action = 'login' ORDER BY id DESC LIMIT 1")
            )
            assert row.scalar_one() == expected_forwarded_hash

        # 2. Wrong X-Internal-Proxy-Secret -> X-Client-IP is ignored
        resp_wrong_secret = await client.post(
            "/api/v1/auth/login",
            json={"email": "member@lab.local", "password": PASSWORD},
            headers={
                "X-Internal-Proxy-Secret": "wrong-secret",
                "X-Client-IP": "203.0.113.7",
            },
        )
        assert resp_wrong_secret.status_code == 200
        async with maker() as session:
            row = await session.execute(
                text("SELECT ip_hash FROM audit_events WHERE action = 'login' ORDER BY id DESC LIMIT 1")
            )
            fallback_hash = row.scalar_one()
            assert fallback_hash != expected_forwarded_hash
            assert fallback_hash == hash_ip("127.0.0.1", jwt_secret_val)

        # 3. Valid secret + invalid X-Client-IP -> ignored, falls back to request.client.host
        resp_bad_ip = await client.post(
            "/api/v1/auth/login",
            json={"email": "member@lab.local", "password": PASSWORD},
            headers={
                "X-Internal-Proxy-Secret": proxy_secret,
                "X-Client-IP": "not-an-ip-address",
            },
        )
        assert resp_bad_ip.status_code == 200
        async with maker() as session:
            row = await session.execute(
                text("SELECT ip_hash FROM audit_events WHERE action = 'login' ORDER BY id DESC LIMIT 1")
            )
            assert row.scalar_one() == hash_ip("127.0.0.1", jwt_secret_val)

        # 4. Dual-bucket login rate limiting writes login_rate_limited audit event
        app.state.settings.rate_limit_enabled = True
        app.state.limiter.redis = fake_redis

        # Alternate two accounts 15 times (8 + 7) from the same IP without affecting each other
        for i in range(15):
            email = "member@lab.local" if i % 2 == 0 else "zhou@lab.local"
            r = await client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": PASSWORD},
                headers={
                    "X-Internal-Proxy-Secret": proxy_secret,
                    "X-Client-IP": "203.0.113.8",
                },
            )
            assert r.status_code == 200

        # Account bucket: 2 more attempts on member@lab.local reach 10, 11th returns 429
        for _ in range(2):
            r = await client.post(
                "/api/v1/auth/login",
                json={"email": "member@lab.local", "password": PASSWORD},
                headers={
                    "X-Internal-Proxy-Secret": proxy_secret,
                    "X-Client-IP": "203.0.113.8",
                },
            )
            assert r.status_code == 200

        r_acct_limited = await client.post(
            "/api/v1/auth/login",
            json={"email": "member@lab.local", "password": PASSWORD},
            headers={
                "X-Internal-Proxy-Secret": proxy_secret,
                "X-Client-IP": "203.0.113.8",
            },
        )
        assert r_acct_limited.status_code == 429
        assert r_acct_limited.json()["error"]["code"] == "rate_limited"

        async with maker() as session:
            row = await session.execute(
                text(
                    "SELECT ip_hash, detail FROM audit_events "
                    "WHERE action = 'login_rate_limited' ORDER BY id DESC LIMIT 1"
                )
            )
            ip_hash_row, detail_row = row.one()
            assert ip_hash_row == hash_ip("203.0.113.8", jwt_secret_val)
            assert detail_row == {"bucket": "account"}
    finally:
        app.state.settings.internal_proxy_secret = original_proxy_secret
        app.state.settings.rate_limit_enabled = original_rate_limit
        app.state.limiter.redis = original_redis
        await fake_redis.aclose()


async def test_pending_document_visibility_and_file_upload_workflow(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    from app.ingest_worker import process_next_pending
    from app.main import app
    from app.models import Document
    from tests.test_ingest import make_pdf_bytes

    admin = await _login(client, "admin@lab.local")
    member = await _login(client, "member@lab.local")
    outsider = await _login(client, "zhou@lab.local")

    # 1. Both text and file -> 400 ambiguous_input
    both_resp = await client.post(
        "/api/v1/documents",
        data={"title": "冲突输入", "text": "文字内容", "visibility": "lab", "collection": "knowledge"},
        files={"file": ("note.txt", b"file content", "text/plain")},
        headers=member,
    )
    assert both_resp.status_code == 400
    assert both_resp.json()["error"]["code"] == "ambiguous_input"

    # 2. Member uploads a lab-visible document without running the worker yet
    doc_id = await _upload(
        client,
        member,
        title="待处理实验记录",
        body="这里是一段没有二级标题的纯文本实验记录，应该被当作 text 格式。",
        visibility="lab",
        process=False,
    )

    # Owner sees the pending document in list
    member_list = (await client.get("/api/v1/documents", headers=member)).json()["documents"]
    owner_doc = next((d for d in member_list if d["id"] == doc_id), None)
    assert owner_doc is not None
    assert owner_doc["status"] == "pending"
    assert owner_doc["source_format"] == "text"

    # Outsider does NOT see the pending document in list even though visibility is 'lab'
    outsider_list = (await client.get("/api/v1/documents", headers=outsider)).json()["documents"]
    assert all(d["id"] != doc_id for d in outsider_list)

    # Process the pending document -> now outsider sees it with status='ready'
    processed = await process_next_pending(app.state.embedder, app.state.settings)
    assert str(processed) == doc_id

    outsider_list_after = (await client.get("/api/v1/documents", headers=outsider)).json()["documents"]
    outsider_doc = next((d for d in outsider_list_after if d["id"] == doc_id), None)
    assert outsider_doc is not None
    assert outsider_doc["status"] == "ready"
    assert outsider_doc["error_code"] is None

    # 3. Verify error_code is only exposed to owner and admin
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(
            text("UPDATE documents SET error_code = 'timeout' WHERE id = CAST(:id AS uuid)"),
            {"id": doc_id},
        )
        await session.commit()

    member_doc_err = next(
        d for d in (await client.get("/api/v1/documents", headers=member)).json()["documents"] if d["id"] == doc_id
    )
    admin_doc_err = next(
        d for d in (await client.get("/api/v1/documents", headers=admin)).json()["documents"] if d["id"] == doc_id
    )
    outsider_doc_err = next(
        d for d in (await client.get("/api/v1/documents", headers=outsider)).json()["documents"] if d["id"] == doc_id
    )
    assert member_doc_err["error_code"] == "timeout"
    assert admin_doc_err["error_code"] == "timeout"
    assert outsider_doc_err["error_code"] is None

    # 4. Upload a 2-page PDF file and verify audit fields + citation heading
    pdf_bytes = make_pdf_bytes(
        [
            "Page 1: General lab orientation and equipment safety overview.",
            "Page 2: Cryogenic nitrogen tank valve pressure limit is 350 kPa.",
        ]
    )
    pdf_resp = await client.post(
        "/api/v1/documents",
        data={"title": "低温手册", "visibility": "lab", "collection": "knowledge"},
        files={"file": ("cryo-manual.pdf", pdf_bytes, "application/pdf")},
        headers=member,
    )
    assert pdf_resp.status_code == 202, pdf_resp.text
    pdf_id = pdf_resp.json()["id"]
    while await process_next_pending(app.state.embedder, app.state.settings) is not None:
        pass

    async with maker() as session:
        upload_audit = (
            await session.execute(
                text(
                    "SELECT detail FROM audit_events WHERE action = 'document_upload' AND resource_id = :rid"
                ),
                {"rid": pdf_id},
            )
        ).scalar_one()
        assert upload_audit["format"] == "pdf"
        assert upload_audit["bytes"] == len(pdf_bytes)

    search_resp = await client.post(
        "/api/v1/search",
        json={"query": "Cryogenic nitrogen tank valve pressure limit 350 kPa", "k": 5},
        headers=member,
    )
    assert search_resp.status_code == 200
    hits = search_resp.json()["hits"]
    assert any(h["document_id"] == pdf_id and "第 2 页" in h["heading"] for h in hits)

    # 5. Delete sets deleted_at without changing status to 'failed'
    del_resp = await client.delete(f"/api/v1/documents/{pdf_id}", headers=member)
    assert del_resp.status_code == 200
    async with maker() as session:
        deleted_doc = await session.get(Document, uuid.UUID(pdf_id))
        assert deleted_doc is not None
        assert deleted_doc.deleted_at is not None
        assert deleted_doc.status == "ready"


async def test_audit_actor_name_and_cursor_pagination(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    admin = await _login(client, "admin@lab.local")
    member = await _login(client, "member@lab.local")
    viewer = await _login(client, "viewer@lab.local")

    # Non-admin cannot view audit
    forbidden = await client.get("/api/v1/audit", headers=member)
    assert forbidden.status_code == 403

    # Trigger several distinct audit events (unknown-email failed login, upload, delete)
    await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@qingheng.lab", "password": "wrong"},
    )
    doc_id = await _upload(
        client,
        member,
        title="审计分页测试文档",
        body="## 第一节\n这是一条测试审计游标分页的记录。",
        visibility="lab",
    )
    await client.delete(f"/api/v1/documents/{doc_id}", headers=member)
    await client.post("/api/v1/auth/logout", headers=viewer)

    # Fetch full list
    full_resp = await client.get("/api/v1/audit?limit=100", headers=admin)
    assert full_resp.status_code == 200, full_resp.text
    full_body = full_resp.json()
    all_events: list[dict[str, object]] = full_body["events"]
    assert len(all_events) >= 6
    assert full_body["next_before_id"] is None

    # Verify actor_name is populated via LEFT JOIN users and emails are never returned
    serialized = json.dumps(all_events, ensure_ascii=False)
    assert "admin@lab.local" not in serialized
    assert "member@lab.local" not in serialized
    assert "viewer@lab.local" not in serialized
    assert "nobody@qingheng.lab" not in serialized
    for ev in all_events:
        assert "email" not in ev
        assert "actor_name" in ev

    actor_names = {ev["actor_name"] for ev in all_events}
    assert "沈昭" in actor_names
    assert "林可" in actor_names
    assert "王敏" in actor_names
    assert None in actor_names  # from login_failed for unknown user

    # Verify actor_id filter
    member_me = (await client.get("/api/v1/auth/me", headers=member)).json()
    member_id = member_me["id"]
    by_actor_resp = await client.get(
        f"/api/v1/audit?actor_id={member_id}&limit=100",
        headers=admin,
    )
    assert by_actor_resp.status_code == 200
    by_actor_events = by_actor_resp.json()["events"]
    assert len(by_actor_events) >= 3
    assert all(
        ev["actor_id"] == member_id and ev["actor_name"] == "林可"
        for ev in by_actor_events
    )

    # Verify cursor pagination (before_id / next_before_id): no duplicates and no missing rows
    paginated_events: list[dict[str, object]] = []
    cursor: int | None = None
    pages = 0
    while True:
        url = "/api/v1/audit?limit=2"
        if cursor is not None:
            url += f"&before_id={cursor}"
        page_resp = await client.get(url, headers=admin)
        assert page_resp.status_code == 200
        page_body = page_resp.json()
        page_items: list[dict[str, object]] = page_body["events"]
        assert len(page_items) <= 2
        if cursor is not None:
            assert all(int(item["id"]) < cursor for item in page_items)  # type: ignore[arg-type]
        paginated_events.extend(page_items)
        pages += 1
        cursor = page_body["next_before_id"]
        if cursor is None:
            break

    assert pages >= 3
    all_ids = [int(ev["id"]) for ev in all_events]  # type: ignore[arg-type]
    paginated_ids = [int(ev["id"]) for ev in paginated_events]  # type: ignore[arg-type]
    assert len(paginated_ids) == len(set(paginated_ids)), "cursor pagination returned duplicate rows"
    assert paginated_ids == all_ids, "cursor pagination missed or reordered rows"


async def test_admin_cli_subcommands_and_prod_bootstrap(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io

    from app.admin_cli import main as cli_main

    admin_email = "firstadmin@qingheng.lab"
    init_secret = "ProdAdmin#2026_Valid"
    reset_secret = "ResetAdmin$2026_Next"

    # 1. Weak password rejected by CLI
    monkeypatch.setattr("sys.stdin", io.StringIO("short"))
    assert (
        cli_main(
            [
                "create-user",
                "--email",
                admin_email,
                "--name",
                "首任管理员",
                "--role",
                "admin",
                "--password-stdin",
            ]
        )
        != 0
    )

    # 2. Create first admin via CLI (without running seed, simulating APP_ENV=prod bootstrap)
    monkeypatch.setattr("sys.stdin", io.StringIO(init_secret + "\n"))
    rc = cli_main(
        [
            "create-user",
            "--email",
            admin_email,
            "--name",
            "首任管理员",
            "--role",
            "admin",
            "--password-stdin",
        ]
    )
    assert rc == 0
    capsys.readouterr()

    # 3. list-users outputs user info without password_hash
    assert cli_main(["list-users"]) == 0
    listed_out = capsys.readouterr().out
    assert admin_email in listed_out
    assert "首任管理员" in listed_out
    assert "$argon2" not in listed_out
    assert init_secret not in listed_out

    # 4. Login as the CLI-created admin and access admin API
    login_resp = await client.post(
        "/api/v1/auth/login",
        json={"email": admin_email, "password": init_secret},
    )
    assert login_resp.status_code == 200
    token = login_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    users_resp = await client.get("/api/v1/admin/users", headers=headers)
    assert users_resp.status_code == 200

    # 5. set-role, deactivate, activate, reset-password via CLI
    assert cli_main(["set-role", "--email", admin_email, "--role", "member"]) == 0
    assert cli_main(["deactivate", "--email", admin_email]) == 0

    # While deactivated, login fails
    deact_login = await client.post(
        "/api/v1/auth/login",
        json={"email": admin_email, "password": init_secret},
    )
    assert deact_login.status_code == 401

    assert cli_main(["activate", "--email", admin_email]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(reset_secret + "\n"))
    assert cli_main(["reset-password", "--email", admin_email, "--password-stdin"]) == 0

    new_login = await client.post(
        "/api/v1/auth/login",
        json={"email": admin_email, "password": reset_secret},
    )
    assert new_login.status_code == 200
    assert new_login.json()["user"]["role"] == "member"

    # 6. Verify CLI audit events have actor_id=None, via='cli', and no secrets
    maker = get_sessionmaker()
    async with maker() as session:
        rows = list(
            await session.execute(
                text(
                    "SELECT action, actor_id, detail FROM audit_events "
                    "WHERE action IN ('user_created', 'user_updated', 'user_password_reset') "
                    "ORDER BY id ASC"
                )
            )
        )
    actions = [r[0] for r in rows]
    assert actions == [
        "user_created",
        "user_updated",
        "user_updated",
        "user_updated",
        "user_password_reset",
    ]
    for action, actor_id, detail in rows:
        assert actor_id is None
        assert detail["via"] == "cli"
        serialized = json.dumps(detail, ensure_ascii=False)
        assert init_secret not in serialized
        assert reset_secret not in serialized
        assert admin_email not in serialized
        if action == "user_updated" and "role_from" in detail:
            assert detail["fields"] == ["role"]
            assert detail["role_from"] == "admin"
            assert detail["role_to"] == "member"


async def test_admin_users_api_crud_and_deactivated_token_revocation(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    admin_headers = await _login(client, "admin@lab.local")
    member_headers = await _login(client, "member@lab.local")

    # 1. Non-admin gets 403 and writes permission_denied audit
    forbidden = await client.get("/api/v1/admin/users", headers=member_headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "forbidden"

    # 2. Admin creates a new user (weak password rejected first)
    new_email = "newresearcher@qingheng.lab"
    bad_pw_resp = await client.post(
        "/api/v1/admin/users",
        json={
            "email": new_email,
            "display_name": "新研究员",
            "role": "member",
            "password": "LabAgent-Demo-" + "2026-Member",
        },
        headers=admin_headers,
    )
    assert bad_pw_resp.status_code == 400
    assert bad_pw_resp.json()["error"]["code"] == "weak_password"

    initial_secret = "Researcher#2026_Init"
    create_resp = await client.post(
        "/api/v1/admin/users",
        json={
            "email": new_email,
            "display_name": "新研究员",
            "role": "member",
            "password": initial_secret,
        },
        headers=admin_headers,
    )
    assert create_resp.status_code == 200
    created_user = create_resp.json()
    new_user_id = created_user["id"]
    assert created_user["email"] == new_email
    assert created_user["role"] == "member"
    assert created_user["is_active"] is True
    assert "password_hash" not in created_user

    # 3. GET /api/v1/admin/users lists the new user without password_hash
    list_resp = await client.get("/api/v1/admin/users", headers=admin_headers)
    assert list_resp.status_code == 200
    users_list = list_resp.json()["users"]
    assert any(u["id"] == new_user_id and "password_hash" not in u for u in users_list)

    # 4. PATCH /api/v1/admin/users/{id} updates display_name and role
    patch_resp = await client.patch(
        f"/api/v1/admin/users/{new_user_id}",
        json={"display_name": "资深研究员", "role": "viewer"},
        headers=admin_headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["display_name"] == "资深研究员"
    assert patch_resp.json()["role"] == "viewer"

    # 5. Admin cannot demote or deactivate themselves -> 400 cannot_modify_self
    me_admin = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()
    admin_id = me_admin["id"]

    demote_self = await client.patch(
        f"/api/v1/admin/users/{admin_id}",
        json={"role": "member"},
        headers=admin_headers,
    )
    assert demote_self.status_code == 400
    assert demote_self.json()["error"]["code"] == "cannot_modify_self"

    deact_self = await client.patch(
        f"/api/v1/admin/users/{admin_id}",
        json={"is_active": False},
        headers=admin_headers,
    )
    assert deact_self.status_code == 400
    assert deact_self.json()["error"]["code"] == "cannot_modify_self"

    # 6. POST /api/v1/admin/users/{id}/password resets password
    updated_secret = "Researcher$2026_Next"
    pw_resp = await client.post(
        f"/api/v1/admin/users/{new_user_id}/password",
        json={"password": updated_secret},
        headers=admin_headers,
    )
    assert pw_resp.status_code == 200

    # User logs in with the new password and obtains an active token
    user_login = await client.post(
        "/api/v1/auth/login",
        json={"email": new_email, "password": updated_secret},
    )
    assert user_login.status_code == 200
    user_token = user_login.json()["access_token"]
    user_headers = {"Authorization": f"Bearer {user_token}"}
    assert (await client.get("/api/v1/auth/me", headers=user_headers)).status_code == 200

    # 7. Deactivating the user immediately invalidates their existing token
    deact_resp = await client.patch(
        f"/api/v1/admin/users/{new_user_id}",
        json={"is_active": False},
        headers=admin_headers,
    )
    assert deact_resp.status_code == 200
    assert deact_resp.json()["is_active"] is False

    me_after_deact = await client.get("/api/v1/auth/me", headers=user_headers)
    assert me_after_deact.status_code == 401
    docs_after_deact = await client.get("/api/v1/documents", headers=user_headers)
    assert docs_after_deact.status_code == 401

    # 8. Verify audit trail contains no field values or passwords
    maker = get_sessionmaker()
    async with maker() as session:
        audit_rows = list(
            await session.execute(
                text(
                    "SELECT action, detail FROM audit_events "
                    "WHERE action IN ('permission_denied', 'user_created', 'user_updated', 'user_password_reset') "
                    "ORDER BY id ASC"
                )
            )
        )
    recorded_actions = {r[0] for r in audit_rows}
    assert {
        "permission_denied",
        "user_created",
        "user_updated",
        "user_password_reset",
    } <= recorded_actions
    blob = json.dumps([r[1] for r in audit_rows], ensure_ascii=False)
    assert "资深研究员" not in blob
    assert new_email not in blob
    assert initial_secret not in blob
    assert updated_secret not in blob


async def test_project_groups_api_upload_grants_and_seed_acl(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    from app.ingest_worker import process_next_pending
    from app.main import app
    from app.models import Document, DocumentACL, Group, GroupMember
    from app.seed import seed
    from sqlalchemy import select

    del people
    admin = await _login(client, "admin@lab.local")
    linke = await _login(client, "member@lab.local")
    zhou = await _login(client, "zhou@lab.local")
    viewer = await _login(client, "viewer@lab.local")

    # 1. Non-admin gets 403 on admin group endpoints
    assert (await client.get("/api/v1/admin/groups", headers=linke)).status_code == 403
    assert (
        await client.post(
            "/api/v1/admin/groups",
            json={"slug": "unauthorized", "name": "越权组"},
            headers=linke,
        )
    ).status_code == 403

    # 2. Admin creates a group via POST /api/v1/admin/groups
    create_grp = await client.post(
        "/api/v1/admin/groups",
        json={"slug": "apollo", "name": "阿波罗项目组"},
        headers=admin,
    )
    assert create_grp.status_code == 200, create_grp.text
    grp_data = create_grp.json()
    apollo_id = grp_data["id"]
    assert grp_data["slug"] == "apollo"
    assert grp_data["name"] == "阿波罗项目组"
    assert grp_data["member_ids"] == []

    # 3. Public GET /api/v1/groups accessible to any logged-in user and returns only slug + name
    pub_groups = await client.get("/api/v1/groups", headers=viewer)
    assert pub_groups.status_code == 200
    pub_items = pub_groups.json()["groups"]
    assert any(item == {"slug": "apollo", "name": "阿波罗项目组"} for item in pub_items)
    for item in pub_items:
        assert set(item.keys()) == {"slug", "name"}

    # 4. PUT /api/v1/admin/groups/{id}/members updates members and records added/removed audit
    linke_id = (await client.get("/api/v1/auth/me", headers=linke)).json()["id"]
    zhou_id = (await client.get("/api/v1/auth/me", headers=zhou)).json()["id"]

    put1 = await client.put(
        f"/api/v1/admin/groups/{apollo_id}/members",
        json={"user_ids": [linke_id, zhou_id]},
        headers=admin,
    )
    assert put1.status_code == 200
    assert put1.json()["added"] == 2
    assert put1.json()["removed"] == 0
    assert set(put1.json()["member_ids"]) == {linke_id, zhou_id}

    put2 = await client.put(
        f"/api/v1/admin/groups/{apollo_id}/members",
        json={"user_ids": [linke_id]},
        headers=admin,
    )
    assert put2.status_code == 200
    assert put2.json()["added"] == 0
    assert put2.json()["removed"] == 1
    assert put2.json()["member_ids"] == [linke_id]

    # 5. Upload restricted document with grant_groups and grant_roles
    up_resp = await client.post(
        "/api/v1/documents",
        data={
            "title": "阿波罗机密设计",
            "text": "## 激光冷却\n阿波罗激光器的锁定频率是 418 THz。",
            "collection": "project",
            "visibility": "restricted",
            "grant_groups": "apollo",
            "grant_roles": "viewer",
        },
        headers=admin,
    )
    assert up_resp.status_code == 202, up_resp.text
    apollo_doc_id = up_resp.json()["id"]
    while await process_next_pending(app.state.embedder, app.state.settings) is not None:
        pass

    # Linke (in group apollo) and viewer (role viewer) can read; Zhou (removed from apollo, role member) cannot
    assert (await client.get(f"/api/v1/documents/{apollo_doc_id}", headers=linke)).status_code == 200
    assert (await client.get(f"/api/v1/documents/{apollo_doc_id}", headers=viewer)).status_code == 200
    assert (await client.get(f"/api/v1/documents/{apollo_doc_id}", headers=zhou)).status_code == 404

    zhou_search = await client.post(
        "/api/v1/search",
        json={"query": "418 THz 阿波罗激光器", "k": 5},
        headers=zhou,
    )
    assert zhou_search.status_code == 200
    assert all(h["document_id"] != apollo_doc_id for h in zhou_search.json()["hits"])

    linke_search = await client.post(
        "/api/v1/search",
        json={"query": "418 THz 阿波罗激光器", "k": 5},
        headers=linke,
    )
    assert linke_search.status_code == 200
    assert any(h["document_id"] == apollo_doc_id for h in linke_search.json()["hits"])

    # 6. Verify group_created and group_members_changed audit events
    maker = get_sessionmaker()
    async with maker() as session:
        grp_audits = list(
            await session.execute(
                text(
                    "SELECT action, detail FROM audit_events "
                    "WHERE action IN ('group_created', 'group_members_changed') "
                    "ORDER BY id ASC"
                )
            )
        )
    assert [r[0] for r in grp_audits] == [
        "group_created",
        "group_members_changed",
        "group_members_changed",
    ]
    assert grp_audits[0][1] == {"slug": "apollo"}
    assert grp_audits[1][1] == {"added": 2, "removed": 0}
    assert grp_audits[2][1] == {"added": 0, "removed": 1}

    # 7. Verify seed() provisions group 'helios' with member 林可 and grants project-helios to group 'helios'
    await seed()
    async with maker() as session:
        helios_grp = await session.scalar(select(Group).where(Group.slug == "helios"))
        assert helios_grp is not None
        helios_members = list(
            await session.scalars(
                select(GroupMember.user_id).where(GroupMember.group_id == helios_grp.id)
            )
        )
        assert [str(uid) for uid in helios_members] == [linke_id]

        helios_doc = await session.scalar(
            select(Document).where(
                Document.source == "seed",
                Document.doc_key == "project-helios",
            )
        )
        assert helios_doc is not None
        helios_acls = list(
            await session.scalars(
                select(DocumentACL).where(DocumentACL.document_id == helios_doc.id)
            )
        )
        assert len(helios_acls) == 1
        assert helios_acls[0].principal_type == "group"
        assert helios_acls[0].principal_id == str(helios_grp.id)
        helios_doc_id = str(helios_doc.id)

    assert (await client.get(f"/api/v1/documents/{helios_doc_id}", headers=linke)).status_code == 200
    assert (await client.get(f"/api/v1/documents/{helios_doc_id}", headers=zhou)).status_code == 404


async def test_document_acl_edit_revokes_chunk_retrieval_immediately(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    from app.ingest_worker import process_next_pending
    from app.main import app

    del people
    admin = await _login(client, "admin@lab.local")
    linke = await _login(client, "member@lab.local")
    zhou = await _login(client, "zhou@lab.local")
    viewer = await _login(client, "viewer@lab.local")

    # Admin creates a project group 'orion' and adds Zhou
    grp_resp = await client.post(
        "/api/v1/admin/groups",
        json={"slug": "orion", "name": "猎户座项目组"},
        headers=admin,
    )
    assert grp_resp.status_code == 200, grp_resp.text
    orion_id = grp_resp.json()["id"]
    zhou_id = (await client.get("/api/v1/auth/me", headers=zhou)).json()["id"]
    assert (
        await client.put(
            f"/api/v1/admin/groups/{orion_id}/members",
            json={"user_ids": [zhou_id]},
            headers=admin,
        )
    ).status_code == 200

    # Linke (owner) uploads a lab-visible document first
    up_resp = await client.post(
        "/api/v1/documents",
        data={
            "title": "猎户座低温超导腔参数",
            "text": "## 谐振频率\n猎户座超导谐振腔的基准频率是 9.41 GHz，品质因数达到 3e10。",
            "collection": "project",
            "visibility": "lab",
        },
        headers=linke,
    )
    assert up_resp.status_code == 202, up_resp.text
    doc_id = up_resp.json()["id"]
    while await process_next_pending(app.state.embedder, app.state.settings) is not None:
        pass

    # Owner and admin see the extra 'acl' field on GET /documents/{id}; non-owner reader does not
    owner_detail = (await client.get(f"/api/v1/documents/{doc_id}", headers=linke)).json()
    assert owner_detail["acl"] == {
        "visibility": "lab",
        "user_emails": [],
        "group_slugs": [],
        "roles": [],
    }
    admin_detail = (await client.get(f"/api/v1/documents/{doc_id}", headers=admin)).json()
    assert "acl" in admin_detail

    zhou_detail = (await client.get(f"/api/v1/documents/{doc_id}", headers=zhou)).json()
    assert "acl" not in zhou_detail

    # Non-owner / non-admin attempting PUT /documents/{id}/acl gets 404 (not_found)
    forbidden_acl = await client.put(
        f"/api/v1/documents/{doc_id}/acl",
        json={
            "visibility": "private",
            "user_emails": [],
            "group_slugs": [],
            "roles": [],
        },
        headers=zhou,
    )
    assert forbidden_acl.status_code == 404
    assert forbidden_acl.json()["error"]["code"] == "not_found"

    # Owner updates ACL to restricted with user_emails=[viewer], group_slugs=['orion'], roles=['viewer']
    put_restricted = await client.put(
        f"/api/v1/documents/{doc_id}/acl",
        json={
            "visibility": "restricted",
            "user_emails": ["viewer@lab.local"],
            "group_slugs": ["orion"],
            "roles": ["viewer"],
        },
        headers=linke,
    )
    assert put_restricted.status_code == 200, put_restricted.text
    assert put_restricted.json()["acl"] == {
        "visibility": "restricted",
        "user_emails": ["viewer@lab.local"],
        "group_slugs": ["orion"],
        "roles": ["viewer"],
    }

    # Both Zhou (via group 'orion') and Viewer (via user/role grant) can retrieve the document's chunks
    zhou_hits_before = (
        await client.post(
            "/api/v1/search",
            json={"query": "9.41 GHz 猎户座超导谐振腔", "k": 5},
            headers=zhou,
        )
    ).json()["hits"]
    assert any(h["document_id"] == doc_id for h in zhou_hits_before)

    viewer_hits_before = (
        await client.post(
            "/api/v1/search",
            json={"query": "9.41 GHz 猎户座超导谐振腔", "k": 5},
            headers=viewer,
        )
    ).json()["hits"]
    assert any(h["document_id"] == doc_id for h in viewer_hits_before)

    # Owner changes document visibility to 'private' via PUT /documents/{id}/acl
    put_private = await client.put(
        f"/api/v1/documents/{doc_id}/acl",
        json={
            "visibility": "private",
            "user_emails": [],
            "group_slugs": [],
            "roles": [],
        },
        headers=linke,
    )
    assert put_private.status_code == 200, put_private.text
    assert put_private.json()["visibility"] == "private"
    assert put_private.json()["acl"] == {
        "visibility": "private",
        "user_emails": [],
        "group_slugs": [],
        "roles": [],
    }

    # Previously authorized users (Zhou and Viewer) immediately cannot retrieve chunks or read the doc
    zhou_hits_after = (
        await client.post(
            "/api/v1/search",
            json={"query": "9.41 GHz 猎户座超导谐振腔", "k": 5},
            headers=zhou,
        )
    ).json()["hits"]
    assert all(h["document_id"] != doc_id for h in zhou_hits_after)

    viewer_hits_after = (
        await client.post(
            "/api/v1/search",
            json={"query": "9.41 GHz 猎户座超导谐振腔", "k": 5},
            headers=viewer,
        )
    ).json()["hits"]
    assert all(h["document_id"] != doc_id for h in viewer_hits_after)

    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=zhou)).status_code == 404
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=viewer)).status_code == 404

    # Owner and admin can still retrieve chunks and read the document
    owner_hits_after = (
        await client.post(
            "/api/v1/search",
            json={"query": "9.41 GHz 猎户座超导谐振腔", "k": 5},
            headers=linke,
        )
    ).json()["hits"]
    assert any(h["document_id"] == doc_id for h in owner_hits_after)
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=linke)).status_code == 200
    assert (await client.get(f"/api/v1/documents/{doc_id}", headers=admin)).status_code == 200

    # Verify acl_changed audit events have detail {"visibility": v, "users": n, "groups": m, "roles": k}
    maker = get_sessionmaker()
    async with maker() as session:
        acl_audits = list(
            await session.execute(
                text(
                    "SELECT resource_id, detail FROM audit_events "
                    "WHERE action = 'acl_changed' ORDER BY id ASC"
                )
            )
        )
    assert len(acl_audits) == 2
    assert acl_audits[0][0] == doc_id
    assert acl_audits[0][1] == {
        "visibility": "restricted",
        "users": 1,
        "groups": 1,
        "roles": 1,
    }
    assert acl_audits[1][0] == doc_id
    assert acl_audits[1][1] == {
        "visibility": "private",
        "users": 0,
        "groups": 0,
        "roles": 0,
    }
async def test_purge_document_scrubs_chunks_acl_and_message_citations_and_delete_conversation(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    del people
    linke = await _login(client, "member@lab.local")
    zhou = await _login(client, "zhou@lab.local")

    nonce = "NONCEPURGE99482716"
    doc_id = await _upload(
        client,
        linke,
        title=f"保密实验记录-{nonce}",
        body=f"## 样品参数-{nonce}\n样品批次 {nonce} 的退火温度设定为 725 摄氏度。",
        visibility="restricted",
        grant_emails="zhou@lab.local",
    )

    # Trigger grounded chat that cites the uploaded document so a message stores the nonce in citations
    chat_resp = await client.post(
        "/api/v1/chat/stream",
        json={"message": f"样品批次 {nonce} 的退火温度是多少？"},
        headers=linke,
    )
    assert chat_resp.status_code == 200
    events = _events(chat_resp.text)
    done = next(ev for ev in events if ev["type"] == "done")
    assert done["refused"] is False
    conversation_id = str(done["conversation_id"])

    conv_before = (
        await client.get(f"/api/v1/conversations/{conversation_id}", headers=linke)
    ).json()
    assistant_msgs_before = [m for m in conv_before["messages"] if m["role"] == "assistant"]
    assert len(assistant_msgs_before) == 1
    assert any(
        c["document_id"] == doc_id
        and nonce in c["excerpt"]
        and nonce in c["title"]
        and nonce in c["heading"]
        for c in assistant_msgs_before[0]["citations"]
    )

    # Non-owner / non-admin cannot purge
    forbidden_purge = await client.delete(
        f"/api/v1/documents/{doc_id}?purge=true", headers=zhou
    )
    assert forbidden_purge.status_code == 403

    # Owner purges the document via DELETE /documents/{id}?purge=true
    purge_resp = await client.delete(
        f"/api/v1/documents/{doc_id}?purge=true", headers=linke
    )
    assert purge_resp.status_code == 200, purge_resp.text
    assert purge_resp.json() == {"status": "purged", "messages_scrubbed": 1}

    # Verify full-text / substring search across documents, chunks, document_acl, and messages.citations finds nothing
    maker = get_sessionmaker()
    async with maker() as session:
        doc_hits = (
            await session.execute(
                text(
                    "SELECT count(*) FROM documents "
                    "WHERE id = CAST(:id AS uuid) OR title ILIKE :pat OR content_text ILIKE :pat"
                ),
                {"id": doc_id, "pat": f"%{nonce}%"},
            )
        ).scalar_one()
        assert int(doc_hits) == 0

        chunk_hits = (
            await session.execute(
                text(
                    "SELECT count(*) FROM chunks "
                    "WHERE document_id = CAST(:id AS uuid) "
                    "OR heading ILIKE :pat OR content ILIKE :pat "
                    "OR content_tsv @@ plainto_tsquery('simple', :nonce)"
                ),
                {"id": doc_id, "pat": f"%{nonce}%", "nonce": nonce},
            )
        ).scalar_one()
        assert int(chunk_hits) == 0

        acl_hits = (
            await session.execute(
                text("SELECT count(*) FROM document_acl WHERE document_id = CAST(:id AS uuid)"),
                {"id": doc_id},
            )
        ).scalar_one()
        assert int(acl_hits) == 0

        citation_hits = (
            await session.execute(
                text("SELECT count(*) FROM messages WHERE citations::text ILIKE :pat"),
                {"pat": f"%{nonce}%"},
            )
        ).scalar_one()
        assert int(citation_hits) == 0

        purge_audit = (
            await session.execute(
                text(
                    "SELECT resource_id, detail FROM audit_events "
                    "WHERE action = 'document_purged' ORDER BY id DESC LIMIT 1"
                )
            )
        ).first()
        assert purge_audit is not None
        assert purge_audit[0] == doc_id
        assert purge_audit[1] == {"messages_scrubbed": 1}

    # Verify the citation element on the conversation message has been scrubbed in-place
    conv_after = (
        await client.get(f"/api/v1/conversations/{conversation_id}", headers=linke)
    ).json()
    assistant_msgs_after = [m for m in conv_after["messages"] if m["role"] == "assistant"]
    matching_citations = [
        c for c in assistant_msgs_after[0]["citations"] if c["document_id"] == doc_id
    ]
    assert len(matching_citations) >= 1
    for cit in matching_citations:
        assert cit["excerpt"] == "[已删除]"
        assert cit["title"] == "[已删除文档]"
        assert cit["heading"] == "[已删除]"

    # DELETE /conversations/{id}: another user gets 404; owner deletes own conversation (cascading messages)
    other_del = await client.delete(
        f"/api/v1/conversations/{conversation_id}", headers=zhou
    )
    assert other_del.status_code == 404

    own_del = await client.delete(
        f"/api/v1/conversations/{conversation_id}", headers=linke
    )
    assert own_del.status_code == 200
    assert own_del.json() == {"status": "deleted"}
    assert (
        await client.get(f"/api/v1/conversations/{conversation_id}", headers=linke)
    ).status_code == 404


async def test_maintenance_task_audit_retention(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    from datetime import UTC, datetime, timedelta

    import fakeredis.aioredis
    from app.config import get_settings
    from app.maintenance import run_maintenance_once
    from app.models import AuditEvent
    from sqlalchemy import select

    del client, people
    settings = get_settings()
    maker = get_sessionmaker()
    async with maker() as session:
        old_ev = AuditEvent(
            created_at=datetime.now(UTC) - timedelta(days=settings.audit_retention_days + 5),
            actor_id=None,
            action="old_audit_event",
            detail={},
        )
        fresh_ev = AuditEvent(
            created_at=datetime.now(UTC) - timedelta(days=2),
            actor_id=None,
            action="fresh_audit_event",
            detail={},
        )
        session.add_all([old_ev, fresh_ev])
        await session.commit()

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    counts = await run_maintenance_once(settings, redis_client=fake_redis)
    assert counts is not None
    assert counts["audit_deleted"] == 1

    # Second call while lock is held returns None without running again
    locked_call = await run_maintenance_once(settings, redis_client=fake_redis)
    assert locked_call is None

    async with maker() as session:
        remaining_actions = list(
            await session.scalars(select(AuditEvent.action))
        )
    assert "old_audit_event" not in remaining_actions
    assert "fresh_audit_event" in remaining_actions
    assert "maintenance_run" in remaining_actions


async def test_maintenance_task_purge_expired_soft_deleted_documents(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    from datetime import UTC, datetime, timedelta

    import fakeredis.aioredis
    from app.config import get_settings
    from app.maintenance import run_maintenance_once
    from app.models import Conversation, Document, Message

    del people
    linke = await _login(client, "member@lab.local")
    nonce_expired = "EXPIREDSOFTDEL8821"
    nonce_recent = "RECENTSOFTDEL8822"

    expired_doc_id = await _upload(
        client,
        linke,
        title=f"过期软删文档-{nonce_expired}",
        body=f"## 章节\n过期软删正文 {nonce_expired}",
        visibility="lab",
    )
    recent_doc_id = await _upload(
        client,
        linke,
        title=f"近期软删文档-{nonce_recent}",
        body=f"## 章节\n近期软删正文 {nonce_recent}",
        visibility="lab",
    )

    maker = get_sessionmaker()
    settings = get_settings()
    linke_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=linke)).json()["id"])

    async with maker() as session:
        exp_doc = await session.get(Document, uuid.UUID(expired_doc_id))
        rec_doc = await session.get(Document, uuid.UUID(recent_doc_id))
        assert exp_doc is not None and rec_doc is not None
        exp_doc.deleted_at = datetime.now(UTC) - timedelta(
            days=settings.deleted_document_retention_days + 2
        )
        rec_doc.deleted_at = datetime.now(UTC) - timedelta(days=5)

        conv = Conversation(user_id=linke_id, title="测试会话")
        session.add(conv)
        await session.flush()
        msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content="引用了过期软删文档",
            refused=False,
            citations=[
                {
                    "document_id": expired_doc_id,
                    "title": f"过期软删文档-{nonce_expired}",
                    "heading": f"过期软删小节-{nonce_expired}",
                    "excerpt": f"过期软删正文 {nonce_expired}",
                }
            ],
        )
        session.add(msg)
        await session.commit()
        msg_id = msg.id

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    counts = await run_maintenance_once(settings, redis_client=fake_redis)
    assert counts is not None
    assert counts["documents_purged"] == 1
    assert counts["messages_scrubbed"] == 1

    async with maker() as session:
        assert await session.get(Document, uuid.UUID(expired_doc_id)) is None
        assert await session.get(Document, uuid.UUID(recent_doc_id)) is not None
        scrubbed_msg = await session.get(Message, msg_id)
        assert scrubbed_msg is not None
        assert scrubbed_msg.citations == [
            {
                "document_id": expired_doc_id,
                "title": "[已删除文档]",
                "heading": "[已删除]",
                "excerpt": "[已删除]",
            }
        ]


async def test_maintenance_task_delete_expired_messages(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    from datetime import UTC, datetime, timedelta

    import fakeredis.aioredis
    from app.config import get_settings
    from app.maintenance import run_maintenance_once
    from app.models import Conversation, Message

    del people
    linke = await _login(client, "member@lab.local")
    linke_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=linke)).json()["id"])
    settings = get_settings()
    maker = get_sessionmaker()

    async with maker() as session:
        conv = Conversation(user_id=linke_id, title="消息保留期会话")
        session.add(conv)
        await session.flush()
        old_msg = Message(
            conversation_id=conv.id,
            role="user",
            content="超期旧消息",
            refused=False,
            citations=[],
            created_at=datetime.now(UTC) - timedelta(days=settings.message_retention_days + 3),
        )
        new_msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content="近期新消息",
            refused=False,
            citations=[],
            created_at=datetime.now(UTC) - timedelta(days=3),
        )
        session.add_all([old_msg, new_msg])
        await session.commit()
        old_msg_id = old_msg.id
        new_msg_id = new_msg.id

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    counts = await run_maintenance_once(settings, redis_client=fake_redis)
    assert counts is not None
    assert counts["messages_deleted"] == 1

    async with maker() as session:
        assert await session.get(Message, old_msg_id) is None
        assert await session.get(Message, new_msg_id) is not None


async def test_maintenance_task_delete_expired_revoked_tokens(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    from datetime import UTC, datetime, timedelta

    import fakeredis.aioredis
    from app.config import get_settings
    from app.maintenance import run_maintenance_once
    from app.models import RevokedToken

    del client, people
    settings = get_settings()
    maker = get_sessionmaker()

    async with maker() as session:
        expired_tok = RevokedToken(
            jti="expired-jti-001",
            expires_at=datetime.now(UTC) - timedelta(hours=2),
        )
        active_tok = RevokedToken(
            jti="active-jti-002",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
        session.add_all([expired_tok, active_tok])
        await session.commit()

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    counts = await run_maintenance_once(settings, redis_client=fake_redis)
    assert counts is not None
    assert counts["revoked_tokens_deleted"] == 1

    async with maker() as session:
        assert await session.get(RevokedToken, "expired-jti-001") is None
        assert await session.get(RevokedToken, "active-jti-002") is not None


async def test_audit_filters_csv_export_and_hmac_key(
    client: AsyncClient, people: dict[str, dict[str, str]]
) -> None:
    import csv
    import io
    from datetime import UTC, datetime, timedelta

    from app.audit import hash_ip, write_audit
    from app.main import app
    from pydantic import SecretStr

    del people
    admin = await _login(client, "admin@lab.local")
    member = await _login(client, "member@lab.local")

    doc_id = await _upload(
        client,
        member,
        title="审计过滤与导出测试",
        body="## 第一节\n测试审计过滤参数和 CSV 导出。",
        visibility="lab",
    )

    # Non-admin cannot export CSV
    forbidden_csv = await client.get("/api/v1/audit/export.csv", headers=member)
    assert forbidden_csv.status_code == 403

    # Filter by resource_id
    by_res = await client.get(
        f"/api/v1/audit?resource_id={doc_id}&limit=50", headers=admin
    )
    assert by_res.status_code == 200, by_res.text
    res_events = by_res.json()["events"]
    assert len(res_events) >= 2
    assert all(ev["resource_id"] == doc_id for ev in res_events)

    # Filter by since / until ISO timestamps
    now = datetime.now(UTC)
    past_iso = (now - timedelta(hours=1)).isoformat()
    future_iso = (now + timedelta(hours=1)).isoformat()

    in_window = await client.get(
        f"/api/v1/audit?resource_id={doc_id}&since={past_iso}&until={future_iso}",
        headers=admin,
    )
    assert in_window.status_code == 200
    assert len(in_window.json()["events"]) == len(res_events)

    empty_window = await client.get(
        f"/api/v1/audit?resource_id={doc_id}&until={(now - timedelta(days=1)).isoformat()}",
        headers=admin,
    )
    assert empty_window.status_code == 200
    assert empty_window.json()["events"] == []

    # Invalid timestamp returns 400
    bad_ts = await client.get("/api/v1/audit?since=not-a-timestamp", headers=admin)
    assert bad_ts.status_code == 400
    assert bad_ts.json()["error"]["code"] == "invalid_timestamp"

    # CSV export with same filters writes audit_export and returns valid CSV
    csv_resp = await client.get(
        f"/api/v1/audit/export.csv?resource_id={doc_id}", headers=admin
    )
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    reader = list(csv.DictReader(io.StringIO(csv_resp.text)))
    assert len(reader) == len(res_events)
    assert all(row["resource_id"] == doc_id for row in reader)
    assert all(len(row["prev_hash"]) == 64 and len(row["row_hash"]) == 64 for row in reader)

    export_audit = await client.get("/api/v1/audit?action=audit_export", headers=admin)
    assert export_audit.status_code == 200
    export_events = export_audit.json()["events"]
    assert len(export_events) >= 1
    assert export_events[0]["detail"] == {"rows": len(res_events)}

    # Verify AUDIT_HMAC_KEY is used by write_audit and rotating it breaks correlation with old IP hashes
    settings = app.state.settings
    old_hmac = settings.audit_hmac_key
    key_v1 = "audit-hmac-key-v1-0123456789-abcdef-xyz"
    key_v2 = "audit-hmac-key-v2-9876543210-fedcba-uvw"
    try:
        settings.audit_hmac_key = SecretStr(key_v1)
        await write_audit(
            settings=settings,
            actor_id=None,
            action="ip_hmac_test_v1",
            ip="198.51.100.42",
            detail={},
        )
        settings.audit_hmac_key = SecretStr(key_v2)
        await write_audit(
            settings=settings,
            actor_id=None,
            action="ip_hmac_test_v2",
            ip="198.51.100.42",
            detail={},
        )
    finally:
        settings.audit_hmac_key = old_hmac

    maker = get_sessionmaker()
    async with maker() as session:
        v1_hash = (
            await session.execute(
                text("SELECT ip_hash FROM audit_events WHERE action = 'ip_hmac_test_v1'")
            )
        ).scalar_one()
        v2_hash = (
            await session.execute(
                text("SELECT ip_hash FROM audit_events WHERE action = 'ip_hmac_test_v2'")
            )
        ).scalar_one()
    assert v1_hash == hash_ip("198.51.100.42", key_v1)
    assert v2_hash == hash_ip("198.51.100.42", key_v2)
    assert v1_hash != v2_hash


async def test_audit_hash_chain_50_concurrent_writes_retention_and_tamper_detection(
    client: AsyncClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import asyncio

    from app.admin_cli import main as cli_main
    from app.audit import write_audit
    from app.main import app

    del client
    settings = app.state.settings

    # 1. 50 concurrent writes via write_audit
    await asyncio.gather(
        *(
            write_audit(
                settings=settings,
                actor_id=None,
                action="concurrent_chain_test",
                resource_type="test",
                resource_id=f"item-{i}",
                ip="192.0.2.10",
                detail={"seq": i},
            )
            for i in range(50)
        )
    )

    assert cli_main(["verify-audit"]) == 0
    out_ok = capsys.readouterr().out
    assert "audit chain ok" in out_ok

    # 2. Retention deletes at the head of the table do NOT count as chain breaks
    maker = get_sessionmaker()
    async with maker() as session:
        ids = list(
            await session.scalars(text("SELECT id FROM audit_events ORDER BY id ASC"))
        )
        assert len(ids) == 50
        cutoff_id = int(ids[5])
        await session.execute(text("SELECT set_config('labagent.audit_retention', 'on', true)"))
        await session.execute(
            text("DELETE FROM audit_events WHERE id < :cutoff_id"),
            {"cutoff_id": cutoff_id},
        )
        await session.commit()

    assert cli_main(["verify-audit"]) == 0
    capsys.readouterr()

    # 3. Bypass trigger to corrupt a middle row and verify CLI pinpoints that exact row id
    target_id = int(ids[25])
    async with maker() as session:
        await session.execute(
            text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only")
        )
        try:
            await session.execute(
                text(
                    "UPDATE audit_events SET detail = '{\"seq\": 99999}'::jsonb WHERE id = :id"
                ),
                {"id": target_id},
            )
            await session.commit()
        finally:
            await session.execute(
                text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only")
            )
            await session.commit()

    assert cli_main(["verify-audit"]) != 0
    broken_out = capsys.readouterr().out
    assert f"id={target_id}" in broken_out
async def test_concurrent_purge_scrubs_both_citations_and_startup_maintenance(
    client: AsyncClient,
    people: dict[str, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.config import get_settings
    from app.maintenance import (
        run_maintenance_worker,
        scrub_missing_document_citations,
    )
    from app.models import Conversation, Message

    del people
    linke = await _login(client, "member@lab.local")
    linke_id = uuid.UUID((await client.get("/api/v1/auth/me", headers=linke)).json()["id"])

    nonce_a = "CONCPURGEAAA111"
    nonce_b = "CONCPURGEBBB222"
    nonce_live = "CONCLIVECCC333"

    doc_a = await _upload(
        client,
        linke,
        title=f"并发擦除A-{nonce_a}",
        body=f"## 章节A-{nonce_a}\n正文A {nonce_a}",
        visibility="lab",
    )
    doc_b = await _upload(
        client,
        linke,
        title=f"并发擦除B-{nonce_b}",
        body=f"## 章节B-{nonce_b}\n正文B {nonce_b}",
        visibility="lab",
    )
    doc_live = await _upload(
        client,
        linke,
        title=f"保留文档C-{nonce_live}",
        body=f"## 章节C-{nonce_live}\n正文C {nonce_live}",
        visibility="lab",
    )

    maker = get_sessionmaker()
    async with maker() as session:
        conv = Conversation(user_id=linke_id, title="并发擦除测试会话")
        session.add(conv)
        await session.flush()
        msg = Message(
            conversation_id=conv.id,
            role="assistant",
            content="同时引用了 A 和 B",
            refused=False,
            citations=[
                {
                    "index": 1,
                    "document_id": doc_a,
                    "title": f"并发擦除A-{nonce_a}",
                    "heading": f"章节A-{nonce_a}",
                    "excerpt": f"正文A {nonce_a}",
                },
                {
                    "index": 2,
                    "document_id": doc_b,
                    "title": f"并发擦除B-{nonce_b}",
                    "heading": f"章节B-{nonce_b}",
                    "excerpt": f"正文B {nonce_b}",
                },
            ],
        )
        session.add(msg)
        await session.commit()
        msg_id = msg.id

    # Concurrently purge doc_a and doc_b via HTTP DELETE ?purge=true
    resp_a, resp_b = await asyncio.gather(
        client.delete(f"/api/v1/documents/{doc_a}?purge=true", headers=linke),
        client.delete(f"/api/v1/documents/{doc_b}?purge=true", headers=linke),
    )
    assert resp_a.status_code == 200, resp_a.text
    assert resp_b.status_code == 200, resp_b.text

    # Both citations must be completely scrubbed (no lost update)
    async with maker() as session:
        refreshed = await session.get(Message, msg_id)
        assert refreshed is not None
        assert refreshed.citations == [
            {
                "index": 1,
                "document_id": doc_a,
                "title": "[已删除文档]",
                "heading": "[已删除]",
                "excerpt": "[已删除]",
            },
            {
                "index": 2,
                "document_id": doc_b,
                "title": "[已删除文档]",
                "heading": "[已删除]",
                "excerpt": "[已删除]",
            },
        ]

        # In-flight chat citation revalidation scrubs purged doc_a while keeping doc_live intact
        revalidated = await scrub_missing_document_citations(
            session,
            [
                {
                    "index": 1,
                    "document_id": doc_a,
                    "title": f"并发擦除A-{nonce_a}",
                    "heading": f"章节A-{nonce_a}",
                    "excerpt": f"正文A {nonce_a}",
                },
                {
                    "index": 2,
                    "document_id": doc_live,
                    "title": f"保留文档C-{nonce_live}",
                    "heading": f"章节C-{nonce_live}",
                    "excerpt": f"正文C {nonce_live}",
                },
            ],
        )
        assert revalidated == [
            {
                "index": 1,
                "document_id": doc_a,
                "title": "[已删除文档]",
                "heading": "[已删除]",
                "excerpt": "[已删除]",
            },
            {
                "index": 2,
                "document_id": doc_live,
                "title": f"保留文档C-{nonce_live}",
                "heading": f"章节C-{nonce_live}",
                "excerpt": f"正文C {nonce_live}",
            },
        ]

    # Verify run_maintenance_worker executes one pass immediately on startup before sleeping
    startup_calls = 0
    stop_event = asyncio.Event()

    async def _fake_once(_settings: object) -> dict[str, int]:
        nonlocal startup_calls
        startup_calls += 1
        stop_event.set()
        return {}

    monkeypatch.setattr("app.maintenance.run_maintenance_once", _fake_once)
    await asyncio.wait_for(
        run_maintenance_worker(get_settings(), stop_event),
        timeout=2.0,
    )
    assert startup_calls == 1


async def test_audit_csv_formula_escape_and_signed_zero_hash_chain(
    client: AsyncClient,
    people: dict[str, dict[str, str]],
) -> None:
    import csv
    import io
    from datetime import UTC, datetime

    from app.audit import canonical_audit_json, verify_audit_chain, write_audit
    from app.main import app

    del people
    admin = await _login(client, "admin@lab.local")
    settings = app.state.settings

    # 1. Canonical JSON normalizes -0.0 (both scalar and list items) to 0.0
    now = datetime.now(UTC)
    canon_neg_zero = canonical_audit_json(
        created_at=now,
        actor_id=None,
        action="signed_zero_test",
        resource_type="test",
        resource_id="res-1",
        request_id="req-1",
        ip_hash=None,
        detail={"score": -0.0, "margins": [-0.0, 0.25], "huge": 1.25e20},
    )
    canon_pos_zero = canonical_audit_json(
        created_at=now,
        actor_id=None,
        action="signed_zero_test",
        resource_type="test",
        resource_id="res-1",
        request_id="req-1",
        ip_hash=None,
        detail={"score": 0.0, "margins": [0.0, 0.25], "huge": 125000000000000000000},
    )
    assert canon_neg_zero == canon_pos_zero
    assert "-0.0" not in canon_neg_zero

    # 2. Planted =HYPERLINK(...) request_id and -0.0 detail round-trip through DB & CSV
    malicious_req_id = '=HYPERLINK("http://evil.example","click")'
    await write_audit(
        settings=settings,
        actor_id=None,
        action="csv_formula_and_neg_zero_test",
        resource_type="@formula_type",
        resource_id="+SUM(A1:A2)",
        request_id=malicious_req_id,
        ip="198.51.100.99",
        detail={"score": -0.0, "margins": [-0.0, 0.5]},
    )

    maker = get_sessionmaker()
    async with maker() as session:
        assert await verify_audit_chain(session) is None

    csv_resp = await client.get(
        "/api/v1/audit/export.csv?action=csv_formula_and_neg_zero_test",
        headers=admin,
    )
    assert csv_resp.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_resp.text)))
    assert len(rows) == 1
    assert rows[0]["request_id"] == f"'{malicious_req_id}"
    assert rows[0]["resource_type"] == "'@formula_type"
    assert rows[0]["resource_id"] == "'+SUM(A1:A2)"





