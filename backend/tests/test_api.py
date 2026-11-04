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

PASSWORD = "YanZheng-Test-Password-2026"


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
