"""Tests for transactional audit hash-chain atomicity, request-id normalization, PII-safe audit detail, and /lab CRUD audit coverage."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import pytest
from app.audit import (
    GENESIS_PREV_HASH,
    AuditRejected,
    compute_row_hash,
    normalize_request_id,
    scrub_audit_str,
    verify_audit_chain,
    write_audit,
)
from app.auth import hash_password
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.importer.fingerprint import person_fp
from app.models import AuditEvent, Document, ImportJob, LabPerson, LabProject, User
from app.rag.redact import contains_email_or_phone, redact_text
from httpx import AsyncClient
from sqlalchemy import event, select
from tests.test_proposal_coldstart import build_synthetic_proposal_docx

SYNTH_PHONE = "138" + "00138000"
SYNTH_EMAIL = "zhangsan" + "@" + "tiangong.edu.cn"


async def _bootstrap_admin_headers(client: AsyncClient) -> tuple[User, dict[str, str]]:
    admin_email = "audit-admin" + "@" + "lab.local"
    admin_pw = "AuditAdmin-SafePw-9021!"
    maker = get_sessionmaker()
    async with maker() as session:
        user = User(
            email=admin_email,
            display_name="审计管理员",
            role="admin",
            password_hash=hash_password(admin_pw),
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": admin_email, "password": admin_pw},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return user, {"Authorization": f"Bearer {token}"}


def test_normalize_request_id_and_scrub_audit_str() -> None:
    assert normalize_request_id(b"req-abc_123.01:ok") == "req-abc_123.01:ok"
    overlong = ("r" * 120).encode("latin-1")
    normalized_long = normalize_request_id(overlong)
    assert len(normalized_long) == 32
    assert normalized_long != "r" * 64

    injected = b"valid-prefix\r\nx-injected: evil"
    normalized_injected = normalize_request_id(injected)
    assert len(normalized_injected) == 32
    assert "\r" not in normalized_injected and "\n" not in normalized_injected

    scrubbed = scrub_audit_str(
        f"申报书_{SYNTH_PHONE}_{SYNTH_EMAIL}_" + ("甲" * 240),
        max_len=120,
    )
    assert SYNTH_PHONE not in scrubbed
    assert SYNTH_EMAIL not in scrubbed
    assert len(scrubbed) <= 120

    cjk_scrubbed = scrub_audit_str(f"联系邮箱{SYNTH_EMAIL}请查收", max_len=120)
    assert SYNTH_EMAIL not in cjk_scrubbed
    assert "[REDACTED_EMAIL]" in cjk_scrubbed

    for punct_prefix in ("+", "-", ".", "_", "%"):
        punct_email = f"{punct_prefix}alerts" + "@" + "example.com"
        assert contains_email_or_phone(f"请发送至 {punct_email} 确认")
        redacted_text, cnt = redact_text(f"请发送至 {punct_email} 确认")
        assert cnt == 1
        assert punct_email not in redacted_text
        assert "alerts" + "@" + "example.com" not in redacted_text
        assert "[REDACTED_EMAIL]" in redacted_text

    sha_with_phone_digits = ("f" + SYNTH_PHONE + ("a1b2" * 13))[:64]
    assert len(sha_with_phone_digits) == 64
    assert scrub_audit_str(sha_with_phone_digits) == sha_with_phone_digits


async def test_overlong_and_malformed_x_request_id_never_break_audit(
    client: AsyncClient,
) -> None:
    _, headers = await _bootstrap_admin_headers(client)

    # 1. Valid X-Request-Id is echoed and recorded verbatim
    valid_id = "trace-2026-audit_01:ok"
    r_ok = await client.post(
        "/api/v1/lab/projects",
        headers={**headers, "X-Request-Id": valid_id},
        json={"title": "合法请求头测试项目", "pi_name": "审计管理员"},
    )
    assert r_ok.status_code == 200, r_ok.text
    assert r_ok.headers.get("x-request-id") == valid_id

    # 2. Overlong (>64 chars) X-Request-Id is replaced by a safe 32-hex UUID instead of 500
    r_long = await client.post(
        "/api/v1/lab/projects",
        headers={**headers, "X-Request-Id": "x" * 256},
        json={"title": "超长请求头防崩测试项目", "pi_name": "审计管理员"},
    )
    assert r_long.status_code == 200, r_long.text
    echoed = r_long.headers.get("x-request-id", "")
    assert len(echoed) == 32

    # 3. Direct write_audit call with >64 char request_id also bounds length safely
    await write_audit(
        settings=get_settings(),
        actor_id=None,
        action="system_test",
        request_id="z" * 180,
        detail={"ok": True},
    )

    async with get_sessionmaker()() as session:
        assert await verify_audit_chain(session) is None


async def test_transactional_audit_rolls_back_domain_write_on_failure(
    client: AsyncClient,
) -> None:
    _, headers = await _bootstrap_admin_headers(client)

    with patch("app.routes_lab.write_audit", side_effect=AuditRejected("injected_audit_failure")):
        with pytest.raises(AuditRejected):
            await client.post(
                "/api/v1/lab/projects",
                headers=headers,
                json={"title": "审计失败应整体回滚项目", "pi_name": "审计管理员"},
            )

    async with get_sessionmaker()() as session:
        leaked = await session.scalar(
            select(LabProject).where(LabProject.title == "审计失败应整体回滚项目")
        )
        assert leaked is None
        assert await verify_audit_chain(session) is None


async def test_write_audit_flushes_dirty_orm_before_advisory_lock(
    client: AsyncClient,
) -> None:
    admin_user, _ = await _bootstrap_admin_headers(client)

    async with get_sessionmaker()() as session:
        doc = Document(
            owner_id=admin_user.id,
            title="锁序验证文档",
            source="upload",
            visibility="private",
            content_text="测试正文",
            status="ready",
        )
        session.add(doc)
        await session.commit()
        doc_id = doc.id

    executed_sql: list[str] = []

    def _capture_sql(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        executed_sql.append(str(statement))

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _capture_sql)
    try:
        async with get_sessionmaker()() as session:
            loaded = await session.get(Document, doc_id)
            assert loaded is not None
            loaded.deleted_at = datetime.now(UTC)
            # Intentionally do NOT call session.flush() in caller: write_audit must flush first
            await write_audit(
                settings=get_settings(),
                actor_id=admin_user.id,
                action="document_delete",
                resource_type="document",
                resource_id=str(doc_id),
                detail={"operation": "delete"},
                session=session,
            )
            await session.commit()
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture_sql)

    update_idx = next(
        i for i, sql in enumerate(executed_sql) if "UPDATE documents" in sql
    )
    lock_idx = next(
        i for i, sql in enumerate(executed_sql) if "pg_advisory_xact_lock" in sql
    )
    assert update_idx < lock_idx


async def test_audit_retention_is_atomic_and_preserves_chain(
    client: AsyncClient,
) -> None:
    # Insert an expired genesis audit event before bootstrapping so retention deletes it
    expired_ts = datetime.now(UTC) - timedelta(days=30)
    settings = get_settings()
    expired_detail = {"seed": "expired"}
    expired_hash = compute_row_hash(
        GENESIS_PREV_HASH,
        created_at=expired_ts,
        actor_id=None,
        action="legacy_event",
        resource_type="system",
        resource_id=None,
        request_id="req-expired",
        ip_hash=None,
        detail=expired_detail,
    )
    async with get_sessionmaker()() as session:
        session.add(
            AuditEvent(
                created_at=expired_ts,
                actor_id=None,
                action="legacy_event",
                resource_type="system",
                resource_id=None,
                request_id="req-expired",
                ip_hash=None,
                detail=expired_detail,
                prev_hash=GENESIS_PREV_HASH,
                row_hash=expired_hash,
            )
        )
        await session.commit()

    _, headers = await _bootstrap_admin_headers(client)

    ret = await client.post(
        "/api/v1/audit/retention",
        headers=headers,
        json={"days": 7},
    )
    assert ret.status_code == 200, ret.text
    assert ret.json()["deleted"] == 1

    async with get_sessionmaker()() as session:
        # Expired row is actually committed as deleted (not rolled back!)
        legacy = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "legacy_event")
        )
        assert legacy is None
        ret_evt = await session.scalar(
            select(AuditEvent)
            .where(AuditEvent.action == "audit_retention")
            .order_by(AuditEvent.id.desc())
        )
        assert ret_evt is not None
        assert ret_evt.detail == {"days": 7, "deleted": 1}
        assert await verify_audit_chain(session) is None
        assert settings is not None


async def test_pii_and_long_names_in_tokens_and_imports_do_not_500(
    client: AsyncClient,
) -> None:
    _, headers = await _bootstrap_admin_headers(client)

    # PAT create & revoke with phone/email in token name
    tok_name = f"pat-{SYNTH_PHONE}-{SYNTH_EMAIL}"
    r_tok = await client.post(
        "/api/v1/tokens",
        headers=headers,
        json={"name": tok_name, "expires_in_days": 30},
    )
    assert r_tok.status_code == 201, r_tok.text
    tok_id = r_tok.json()["id"]

    r_rev = await client.delete(f"/api/v1/tokens/{tok_id}", headers=headers)
    assert r_rev.status_code == 200, r_rev.text

    # Proposal upload & delete with phone + email + >200 chars in filename
    noisy_filename = f"申报书_{SYNTH_PHONE}_{SYNTH_EMAIL}_{'研' * 210}.docx"
    docx_bytes = build_synthetic_proposal_docx()
    r_imp = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=headers,
        files={
            "file": (
                noisy_filename,
                docx_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r_imp.status_code == 200, r_imp.text
    job_id = r_imp.json()["job_id"]

    r_del = await client.delete(f"/api/v1/admin/imports/{job_id}", headers=headers)
    assert r_del.status_code == 200, r_del.text

    async with get_sessionmaker()() as session:
        assert await verify_audit_chain(session) is None
        events = list(
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action.in_(
                        (
                            "token_create",
                            "token_revoke",
                            "import_proposal_parse",
                            "import_job_deleted",
                        )
                    )
                )
            )
        )
        assert {e.action for e in events} == {
            "token_create",
            "token_revoke",
            "import_proposal_parse",
            "import_job_deleted",
        }
        for ev in events:
            serialized_detail = str(ev.detail)
            assert SYNTH_PHONE not in serialized_detail
            assert SYNTH_EMAIL not in serialized_detail


async def test_all_lab_crud_mutations_record_hash_chained_audits(
    client: AsyncClient,
) -> None:
    admin_user, headers = await _bootstrap_admin_headers(client)

    # Seed one ImportJob + LabPerson so PATCH /lab/people/{id} can also be verified
    async with get_sessionmaker()() as session:
        seed_job = ImportJob(
            uploaded_by=admin_user.id,
            original_filename="seed.docx",
            content_sha256="a" * 64,
            parser_version="v2.1-fsm",
            as_of_time=date(2026, 6, 1),
            status="provisioned",
            preview={},
            stats={},
        )
        session.add(seed_job)
        await session.flush()
        person = LabPerson(
            user_id=admin_user.id,
            name="原名测试",
            project_role="applicant",
            affiliation="人工智能实验室",
            as_of_time=date(2026, 6, 1),
            source_import_id=seed_job.id,
            entity_fingerprint=person_fp(
                tier="T2",
                login_name=None,
                name="原名测试",
                affiliation="人工智能实验室",
                email=None,
            ),
        )
        session.add(person)
        await session.commit()
        await session.refresh(person)
        person_id = str(person.id)

    # 1. PATCH /lab/people/{id} (including name)
    r = await client.patch(
        f"/api/v1/lab/people/{person_id}",
        headers=headers,
        json={"name": "新名测试", "title": "教授"},
    )
    assert r.status_code == 200, r.text

    # 2. Projects CRUD
    r_proj = await client.post(
        "/api/v1/lab/projects",
        headers=headers,
        json={"title": "国家重点研发子课题", "pi_name": "新名测试", "status": "ongoing"},
    )
    assert r_proj.status_code == 200, r_proj.text
    proj_id = r_proj.json()["project"]["id"]

    r_proj_u = await client.patch(
        f"/api/v1/lab/projects/{proj_id}",
        headers=headers,
        json={"funder": "科技部"},
    )
    assert r_proj_u.status_code == 200, r_proj_u.text

    r_proj_d = await client.delete(f"/api/v1/lab/projects/{proj_id}", headers=headers)
    assert r_proj_d.status_code == 200, r_proj_d.text

    # 3. Achievements CRUD
    r_ach = await client.post(
        "/api/v1/lab/achievements",
        headers=headers,
        json={"kind": "paper", "title": "大模型形式化验证研究", "first_author": "新名测试", "year": 2026},
    )
    assert r_ach.status_code == 200, r_ach.text
    ach_id = r_ach.json()["achievement"]["id"]

    r_ach_u = await client.patch(
        f"/api/v1/lab/achievements/{ach_id}",
        headers=headers,
        json={"venue": "NeurIPS"},
    )
    assert r_ach_u.status_code == 200, r_ach_u.text

    r_ach_d = await client.delete(f"/api/v1/lab/achievements/{ach_id}", headers=headers)
    assert r_ach_d.status_code == 200, r_ach_d.text

    # 4. Facilities CRUD
    r_fac = await client.post(
        "/api/v1/lab/facilities",
        headers=headers,
        json={"category": "compute", "name": "H100 智算集群", "spec": "8x80GB"},
    )
    assert r_fac.status_code == 200, r_fac.text
    fac_id = r_fac.json()["facility"]["id"]

    r_fac_u = await client.patch(
        f"/api/v1/lab/facilities/{fac_id}",
        headers=headers,
        json={"usage_scenario": "大模型预训练与推理评测"},
    )
    assert r_fac_u.status_code == 200, r_fac_u.text

    r_fac_d = await client.delete(f"/api/v1/lab/facilities/{fac_id}", headers=headers)
    assert r_fac_d.status_code == 200, r_fac_d.text

    # 5. Reindex lab profile
    r_reidx = await client.post("/api/v1/lab/reindex", headers=headers)
    assert r_reidx.status_code == 200, r_reidx.text

    expected_actions = {
        "lab_person_updated",
        "lab_project_created",
        "lab_project_updated",
        "lab_project_deleted",
        "lab_achievement_created",
        "lab_achievement_updated",
        "lab_achievement_deleted",
        "lab_facility_created",
        "lab_facility_updated",
        "lab_facility_deleted",
        "lab_profile_reindexed",
    }

    async with get_sessionmaker()() as session:
        assert await verify_audit_chain(session) is None
        rows = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action.in_(expected_actions))
            )
        )
        recorded = {row.action for row in rows}
        assert recorded == expected_actions
        person_ev = next(r for r in rows if r.action == "lab_person_updated")
        assert "name" in (person_ev.detail or {}).get("fields", [])
