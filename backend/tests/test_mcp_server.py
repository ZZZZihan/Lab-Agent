"""Tests for Spec T4.1 / PR-28: MCP server hardening, ACL enforcement, audit logging, and answer_question."""

from __future__ import annotations

import json
import uuid

import pytest
from app.auth import hash_password
from app.config import get_settings, reset_settings_cache
from app.db import get_sessionmaker
from app.ingest_worker import process_next_pending
from app.mcp_server import (
    TOOL_NAMES,
    build_server,
    close_mcp_singletons,
    get_mcp_embedder,
    get_mcp_llm,
    get_mcp_settings,
    mcp_answer_question,
    mcp_fetch_document,
    mcp_get_document_excerpt,
    mcp_list_collections,
    mcp_query_lab_projects,
    mcp_query_meetings,
    mcp_search,
    mcp_search_knowledge_base,
    mcp_search_papers,
    reset_mcp_singletons,
)
from app.models import AuditEvent, Document, DocumentACL, Group, GroupMember, User
from app.rag.embedder import build_embedder
from sqlalchemy import select


async def _drain_worker() -> None:
    settings = get_settings()
    embedder = build_embedder(settings)
    try:
        while await process_next_pending(embedder, settings) is not None:
            pass
    finally:
        await embedder.aclose()


async def test_mcp_lazy_singletons_and_server_registration() -> None:
    reset_mcp_singletons()
    try:
        server = build_server()
        assert server is not None
        assert "search_knowledge_base" in TOOL_NAMES
        assert "list_collections" in TOOL_NAMES
        assert "get_document_excerpt" in TOOL_NAMES
        assert "fetch_document" in TOOL_NAMES
        assert "answer_question" in TOOL_NAMES
        assert mcp_search is mcp_search_knowledge_base

        s1 = get_mcp_settings()
        s2 = get_mcp_settings()
        assert s1 is s2

        e1 = get_mcp_embedder()
        e2 = get_mcp_embedder()
        assert e1 is e2

        l1 = get_mcp_llm()
        l2 = get_mcp_llm()
        assert l1 is l2

        await close_mcp_singletons()
        e3 = get_mcp_embedder()
        assert e3 is not e1
    finally:
        await close_mcp_singletons()


async def test_mcp_actor_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_mcp_singletons()
    maker = get_sessionmaker()
    inactive_id = uuid.uuid4()
    async with maker() as session:
        session.add(
            User(
                id=inactive_id,
                email="inactive@lab.test",
                display_name="停用账号",
                role="member",
                password_hash=hash_password("LabAgent-Test-Inactive-2026!"),
                is_active=False,
            )
        )
        await session.commit()

    monkeypatch.delenv("MCP_ACTOR_EMAIL", raising=False)
    with pytest.raises(RuntimeError, match="MCP_ACTOR_EMAIL is required"):
        await mcp_list_collections()

    monkeypatch.setenv("MCP_ACTOR_EMAIL", "missing@lab.test")
    with pytest.raises(RuntimeError, match="MCP actor is not provisioned"):
        await mcp_list_collections()

    monkeypatch.setenv("MCP_ACTOR_EMAIL", "inactive@lab.test")
    with pytest.raises(RuntimeError, match="MCP actor is not provisioned"):
        await mcp_list_collections()


async def test_mcp_acl_enforcement_and_group_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_mcp_singletons()
    maker = get_sessionmaker()

    admin_id = uuid.uuid4()
    member_id = uuid.uuid4()
    viewer_id = uuid.uuid4()
    group_id = uuid.uuid4()

    lab_doc_id = uuid.uuid4()
    private_doc_id = uuid.uuid4()
    group_doc_id = uuid.uuid4()
    ungranted_doc_id = uuid.uuid4()

    async with maker() as session:
        session.add_all(
            [
                User(
                    id=admin_id,
                    email="admin@lab.local",
                    display_name="沈昭",
                    role="admin",
                    password_hash=hash_password("LabAgent-Demo-2026-Admin"),
                    is_active=True,
                ),
                User(
                    id=member_id,
                    email="member@lab.local",
                    display_name="林砚",
                    role="member",
                    password_hash=hash_password("LabAgent-Demo-2026-Member"),
                    is_active=True,
                ),
                User(
                    id=viewer_id,
                    email="viewer@lab.test",
                    display_name="周澄",
                    role="viewer",
                    password_hash=hash_password("LabAgent-Demo-2026-Viewer"),
                    is_active=True,
                ),
                Group(
                    id=group_id,
                    slug="hpc-team",
                    name="高性能计算组",
                ),
            ]
        )
        await session.flush()
        session.add(GroupMember(group_id=group_id, user_id=member_id))
        session.add_all(
            [
                Document(
                    id=lab_doc_id,
                    owner_id=admin_id,
                    title="清衡集群公开使用手册",
                    collection="knowledge",
                    source="upload",
                    source_format="markdown",
                    visibility="lab",
                    status="pending",
                    content_text="## 作业提交\n\n清衡集群默认分区为 qh-gpu，单次作业最长运行 72 小时。",
                ),
                Document(
                    id=private_doc_id,
                    owner_id=admin_id,
                    title="实验室主任私人备忘录",
                    collection="knowledge",
                    source="upload",
                    source_format="markdown",
                    visibility="private",
                    status="pending",
                    content_text="## 核心机密\n\n玄霜网关应急恢复口令存放于三号保险柜暗格。",
                ),
                Document(
                    id=group_doc_id,
                    owner_id=admin_id,
                    title="HPC组内部节点维护排期",
                    collection="project",
                    source="upload",
                    source_format="markdown",
                    visibility="restricted",
                    status="pending",
                    content_text="## 节点维护\n\n曜石存储节点将于本周六凌晨两点进行固件升级。",
                ),
                Document(
                    id=ungranted_doc_id,
                    owner_id=admin_id,
                    title="未授权受限会议纪要",
                    collection="meeting",
                    source="upload",
                    source_format="markdown",
                    visibility="restricted",
                    status="pending",
                    content_text="## 专项评审\n\n赤霄专项经费预算仅对特定评审人可见。",
                ),
            ]
        )
        await session.flush()
        session.add(
            DocumentACL(
                document_id=group_doc_id,
                principal_type="group",
                principal_id=str(group_id),
                permission="read",
            )
        )
        await session.commit()

    await _drain_worker()

    # 1. Viewer (not in hpc-team) can only see the lab document
    monkeypatch.setenv("MCP_ACTOR_EMAIL", "viewer@lab.test")
    cols_viewer = json.loads(await mcp_list_collections())
    assert cols_viewer["status"] == "ok"
    assert cols_viewer["total_documents"] == 1
    by_col_viewer = {c["collection"]: c["document_count"] for c in cols_viewer["collections"]}
    assert by_col_viewer["knowledge"] == 1
    assert by_col_viewer["project"] == 0

    # Viewer cannot search private or group-restricted documents
    s_priv_viewer = json.loads(await mcp_search_knowledge_base("玄霜网关三号保险柜"))
    assert all(h["document_id"] != str(private_doc_id) for h in s_priv_viewer["hits"])
    s_grp_viewer = json.loads(await mcp_search_knowledge_base("曜石存储节点固件升级"))
    assert all(h["document_id"] != str(group_doc_id) for h in s_grp_viewer["hits"])

    # Viewer cannot excerpt or fetch private or restricted documents
    ex_priv = json.loads(await mcp_get_document_excerpt(str(private_doc_id), max_chars=120))
    assert ex_priv == {"status": "not_found"}
    ex_grp = json.loads(await mcp_get_document_excerpt(str(group_doc_id), max_chars=120))
    assert ex_grp == {"status": "not_found"}
    fetch_ungranted = json.loads(await mcp_fetch_document(str(ungranted_doc_id)))
    assert fetch_ungranted == {"status": "not_found"}

    # Viewer can excerpt the lab document
    ex_lab = json.loads(await mcp_get_document_excerpt(str(lab_doc_id), max_chars=25))
    assert ex_lab["status"] == "ok"
    assert ex_lab["document_id"] == str(lab_doc_id)
    assert len(ex_lab["excerpt"]) <= 25

    # 2. Member (in hpc-team) can read lab + group-restricted documents, but NOT private or ungranted restricted
    monkeypatch.setenv("MCP_ACTOR_EMAIL", "member@lab.local")
    cols_member = json.loads(await mcp_list_collections())
    assert cols_member["total_documents"] == 2
    by_col_member = {c["collection"]: c["document_count"] for c in cols_member["collections"]}
    assert by_col_member["knowledge"] == 1
    assert by_col_member["project"] == 1
    assert by_col_member["meeting"] == 0

    s_grp_member = json.loads(await mcp_query_lab_projects("曜石存储节点固件升级"))
    assert s_grp_member["status"] == "ok"
    assert len(s_grp_member["hits"]) == 1
    assert "曜石存储节点" in s_grp_member["hits"][0]["excerpt"]

    ex_grp_member = json.loads(await mcp_get_document_excerpt(str(group_doc_id)))
    assert ex_grp_member["status"] == "ok"
    assert ex_grp_member["document_id"] == str(group_doc_id)

    ex_priv_member = json.loads(await mcp_get_document_excerpt(str(private_doc_id)))
    assert ex_priv_member == {"status": "not_found"}


async def test_mcp_answer_question_and_audit_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVIDENCE_SCORE_MIN", "0.20")
    reset_settings_cache()
    reset_mcp_singletons()
    maker = get_sessionmaker()

    admin_id = uuid.uuid4()
    member_id = uuid.uuid4()
    lab_doc_id = uuid.uuid4()
    private_doc_id = uuid.uuid4()
    meeting_doc_id = uuid.uuid4()

    async with maker() as session:
        session.add_all(
            [
                User(
                    id=admin_id,
                    email="admin@lab.local",
                    display_name="沈昭",
                    role="admin",
                    password_hash=hash_password("LabAgent-Demo-2026-Admin"),
                    is_active=True,
                ),
                User(
                    id=member_id,
                    email="member@lab.local",
                    display_name="林砚",
                    role="member",
                    password_hash=hash_password("LabAgent-Demo-2026-Member"),
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
                    content_text="## 分区与配额\n\n清衡集群默认分区为 qh-gpu，单用户最大并发作业数为 16 个。",
                ),
                Document(
                    id=private_doc_id,
                    owner_id=admin_id,
                    title="管理员私密密钥记录",
                    collection="knowledge",
                    source="upload",
                    source_format="markdown",
                    visibility="private",
                    status="pending",
                    content_text="## 离线根密钥\n\n离线根密钥保险箱代号为苍穹七号，密码由沈昭保管。",
                ),
                Document(
                    id=meeting_doc_id,
                    owner_id=admin_id,
                    title="秋季组会纪要",
                    collection="meeting",
                    source="upload",
                    source_format="markdown",
                    visibility="lab",
                    status="pending",
                    content_text="## 组会安排\n\n每周三下午三点在 402 会议室召开组会。",
                ),
            ]
        )
        await session.commit()

    await _drain_worker()
    monkeypatch.setenv("MCP_ACTOR_EMAIL", "member@lab.local")

    # 1. Answerable question over lab document returns grounded answer and citations
    ans_raw = await mcp_answer_question("清衡集群默认分区是什么？单用户最大并发作业数是多少？")
    ans = json.loads(ans_raw)
    assert ans["refused"] is False
    assert ans["reason"] is None
    assert "qh-gpu" in ans["answer"]
    assert "[1]" in ans["answer"]
    assert len(ans["citations"]) >= 1
    first_cite = ans["citations"][0]
    assert set(first_cite.keys()) == {"title", "heading", "document_id", "excerpt"}
    assert first_cite["document_id"] == str(lab_doc_id)
    assert first_cite["title"] == "清衡集群使用指南"

    # 2. Question targeting admin's private document is refused for member
    refused_priv_raw = await mcp_answer_question("离线根密钥保险箱代号苍穹七号由谁保管？")
    refused_priv = json.loads(refused_priv_raw)
    assert refused_priv["refused"] is True
    assert refused_priv["reason"] in {"low_score", "no_hits", "low_rare_coverage"}
    assert refused_priv["answer"] == ""
    assert refused_priv["citations"] == []

    # 3. Exercise remaining MCP tools so all are audited
    meetings_res = json.loads(await mcp_query_meetings("402 会议室组会"))
    assert meetings_res["status"] == "ok"
    assert len(meetings_res["hits"]) >= 1

    papers_res = json.loads(await mcp_search_papers("transformer"))
    assert papers_res["status"] == "not_configured"

    # 4. Verify AuditEvent records have action == "tool_call" and detail["channel"] == "mcp"
    async with maker() as session:
        rows = list(
            await session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.action == "tool_call",
                    AuditEvent.resource_type == "mcp_tool",
                    AuditEvent.actor_id == member_id,
                )
                .order_by(AuditEvent.id.asc())
            )
        )
        assert len(rows) >= 4
        tools_seen = [str(r.detail.get("tool")) for r in rows]
        assert "answer_question" in tools_seen
        assert "query_meetings" in tools_seen
        assert "search_papers" in tools_seen

        for row in rows:
            assert row.request_id is not None and row.request_id.startswith("mcp-")
            assert row.detail.get("channel") == "mcp"
            assert row.resource_id == row.detail.get("tool")
            assert isinstance(row.detail.get("status"), str)
            assert isinstance(row.detail.get("hit_count"), int)

        ans_events = [r for r in rows if r.detail.get("tool") == "answer_question"]
        statuses = {str(r.detail.get("status")) for r in ans_events}
        assert statuses == {"ok", "refused"}

    await close_mcp_singletons()
    reset_settings_cache()
