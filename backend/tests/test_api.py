"""API checks for login, ACL at retrieval time, grounded refusal, and audit."""

from __future__ import annotations

import json
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
) -> str:
    form = {
        "title": title,
        "text": body,
        "visibility": visibility,
        "collection": collection,
    }
    if grant_emails:
        form["grant_emails"] = grant_emails
    response = await client.post("/api/v1/documents", data=form, headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


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




