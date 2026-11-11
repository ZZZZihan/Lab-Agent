"""Regression tests for nullable source_import_id, revert/delete cascade safety, orphan FieldProvenance cleanup, read-only merge-preview, and ImportJob.preview deferred loading."""

from __future__ import annotations

import io
import uuid
from typing import Any

import docx
from app.auth import hash_password
from app.db import get_engine, get_sessionmaker
from app.models import (
    FieldProvenance,
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
    User,
)
from httpx import AsyncClient
from sqlalchemy import delete, event, select

ADMIN_EMAIL = "admin@lab.local"
ADMIN_PASSWORD = "LabAgent-Test-Password-2026"
INITIAL_PW = "LabAgent@2026"


def _build_proposal_docx(
    *,
    fill_date_str: str = "2025 年 6 月 1 日",
    project_title: str = "可信多模态智能评测关键技术研究",
    wan_title: str = "教授",
    wan_tutor: str = "博士生导师",
    extra_grant_no: str | None = None,
) -> bytes:
    doc = docx.Document()
    t1 = doc.add_table(rows=5, cols=2)
    for idx, (k, v) in enumerate(
        [
            ("项 目 名 称", project_title),
            ("申 请 人", "万振凯"),
            ("所 在 单 位", "天津天狮学院"),
            ("通 讯 地 址", "天津市武清区"),
            ("填 表 日 期", fill_date_str),
        ]
    ):
        t1.rows[idx].cells[0].text = k
        t1.rows[idx].cells[1].text = v

    t2 = doc.add_table(rows=5, cols=10)
    t2.rows[0].cells[0].text = "申请人基本情况"
    for c_idx, val in enumerate(
        ["姓名", "万振凯", "性别", "男", "出生年月", "197001", "专业技术职务", wan_title, "行政职务", ""]
    ):
        t2.rows[1].cells[c_idx].text = val
    for c_idx, val in enumerate(
        ["最终学位", "博士", "研究专长", "智能评卷", "担任导师", wan_tutor, "所在单位", "天津天狮学院", "电子信箱", ""]
    ):
        t2.rows[2].cells[c_idx].text = val
    for c_idx, val in enumerate(
        ["证件类型", "身份证", "证件号码", "", "联系电话", "", "", "", "", ""]
    ):
        t2.rows[3].cells[c_idx].text = val
    for c_idx, val in enumerate(
        ["序号", "姓名", "性别", "出生年月", "专业技术职务", "行政职务", "研究专长", "最终学位", "所在单位", "证件号码"]
    ):
        t2.rows[4].cells[c_idx].text = val

    proj_line_base = (
        "（1）《基础智能阅卷平台构建》（AGE240005），万振凯（主持），全国教育科学规划课题，2024.01—2025.12，经费 120 万元。"
        if fill_date_str.startswith("2026")
        else "（1）《基础智能阅卷平台构建》（AGE240005），万振凯（主持），全国教育科学规划课题，2024年1月立项，经费 120 万元，在研。"
    )
    proj_lines = [proj_line_base]
    if extra_grant_no:
        proj_lines.append(
            f"（2）《增量专项课题-{extra_grant_no}》（{extra_grant_no}），万振凯（主持），市级科研专项，2025年1月立项，经费 80 万元，在研。"
        )
    ach_lines = [
        "（1）万振凯，《面向开放问答题的多维评分一致性模型》，计算机研究与发展，2025年。"
    ]
    t3 = doc.add_table(rows=1, cols=1)
    t3.rows[0].cells[0].text = (
        "前期研究基础\n"
        f"1．[学术简历] 万振凯，天津天狮学院{wan_title}。\n"
        "2．[前期成果]\n"
        + "\n".join(ach_lines)
        + "\n3．[承担项目]\n"
        + "\n".join(proj_lines)
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def _seed_admin_and_login(client: AsyncClient) -> tuple[User, dict[str, str]]:
    maker = get_sessionmaker()
    async with maker() as session:
        admin = User(
            email=ADMIN_EMAIL,
            display_name="沈昭",
            role="admin",
            password_hash=hash_password(ADMIN_PASSWORD),
            is_active=True,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)

    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return admin, {"Authorization": f"Bearer {token}"}


async def _upload_job(
    client: AsyncClient,
    headers: dict[str, str],
    docx_bytes: bytes,
    filename: str,
    as_of_time: str,
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=headers,
        files={
            "file": (
                filename,
                docx_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        data={"as_of_time": as_of_time},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_manual_assets_detached_from_imports_and_reject_restore_imported(
    client: AsyncClient,
) -> None:
    """Manual entities created before or after imports have source_import_id=None, fabricate no dummy ImportJob, report has_manual_override=False, and reject restore-imported."""
    _, headers = await _seed_admin_and_login(client)

    # 1. Create manual assets when NO ImportJob exists yet -> no fake manual-entry.docx job created
    r_proj = await client.post(
        "/api/v1/lab/projects",
        headers=headers,
        json={
            "title": "纯手工录入横向项目",
            "grant_no": "MAN-PURE-01",
            "funder": "企业委托",
            "pi_name": "沈昭",
            "amount_wan": "50.00",
            "status": "ongoing",
        },
    )
    assert r_proj.status_code == 200, r_proj.text
    proj = r_proj.json()["project"]
    assert proj["source_import_id"] is None
    assert proj["provenance"]["title"]["source_kind"] == "manual"
    assert proj["provenance"]["title"]["has_manual_override"] is False

    maker = get_sessionmaker()
    async with maker() as session:
        jobs_in_db = list(await session.scalars(select(ImportJob)))
        assert jobs_in_db == []

    # Calling restore-imported on a purely manual entity is rejected with 409 no_imported_baseline
    r_restore = await client.post(
        f"/api/v1/lab/projects/{proj['id']}/restore-imported",
        headers=headers,
        json={"field": "funder"},
    )
    assert r_restore.status_code == 409, r_restore.text
    assert r_restore.json()["error"]["code"] == "no_imported_baseline"

    # 2. Provision a real import job, merge a second import job, then create another manual asset
    job_a = await _upload_job(
        client,
        headers,
        _build_proposal_docx(fill_date_str="2024 年 6 月 1 日", wan_title="副教授"),
        "proposal-2024.docx",
        "2024-06-01",
    )
    r_prov_a = await client.post(
        f"/api/v1/admin/imports/{job_a['job_id']}/provision",
        headers=headers,
        json={"initial_password": INITIAL_PW},
    )
    assert r_prov_a.status_code == 200, r_prov_a.text

    job_b = await _upload_job(
        client,
        headers,
        _build_proposal_docx(
            fill_date_str="2025 年 6 月 1 日",
            wan_title="教授",
            extra_grant_no="INC-2025-99",
        ),
        "proposal-2025.docx",
        "2025-06-01",
    )
    r_merge_b = await client.post(
        f"/api/v1/admin/imports/{job_b['job_id']}/merge",
        headers=headers,
        json={},
    )
    assert r_merge_b.status_code == 200, r_merge_b.text

    # Create manual achievement & facility AFTER job_b is merged -> must NOT attach to job_b or job_a
    r_ach = await client.post(
        "/api/v1/lab/achievements",
        headers=headers,
        json={
            "kind": "paper",
            "title": "纯手工代表作论文",
            "first_author": "沈昭",
            "year": 2026,
        },
    )
    assert r_ach.status_code == 200, r_ach.text
    ach = r_ach.json()["achievement"]
    assert ach["source_import_id"] is None

    r_fac = await client.post(
        "/api/v1/lab/facilities",
        headers=headers,
        json={
            "category": "compute",
            "name": "纯手工录入推理服务器",
            "spec": "4xH20",
        },
    )
    assert r_fac.status_code == 200, r_fac.text
    fac = r_fac.json()["facility"]
    assert fac["source_import_id"] is None

    # Also manually edit the project created by job_b (INC-2025-99) so it has manual provenance
    projs_list = (await client.get("/api/v1/lab/projects", headers=headers)).json()[
        "projects"
    ]
    inc_proj = next(p for p in projs_list if p["grant_no"] == "INC-2025-99")
    assert inc_proj["source_import_id"] == job_b["job_id"]
    r_patch_inc = await client.patch(
        f"/api/v1/lab/projects/{inc_proj['id']}",
        headers=headers,
        json={"amount_wan": "188.00"},
    )
    assert r_patch_inc.status_code == 200, r_patch_inc.text

    # 3. Revert job_b AND then DELETE job_b -> both succeed, and manual assets + manually-edited inc_proj survive with source_import_id=None!
    r_rev_b = await client.post(
        f"/api/v1/admin/imports/{job_b['job_id']}/revert",
        headers=headers,
    )
    assert r_rev_b.status_code == 200, r_rev_b.text
    assert r_rev_b.json()["job_status"] == "parsed"

    r_del_b = await client.delete(
        f"/api/v1/admin/imports/{job_b['job_id']}",
        headers=headers,
    )
    assert r_del_b.status_code == 200, r_del_b.text

    projs_after = (await client.get("/api/v1/lab/projects", headers=headers)).json()[
        "projects"
    ]
    achs_after = (
        await client.get("/api/v1/lab/achievements", headers=headers)
    ).json()["achievements"]
    facs_after = (await client.get("/api/v1/lab/facilities", headers=headers)).json()[
        "facilities"
    ]
    assert any(p["id"] == proj["id"] and p["source_import_id"] is None for p in projs_after)
    survived_inc = next(p for p in projs_after if p["id"] == inc_proj["id"])
    assert survived_inc["source_import_id"] is None
    assert survived_inc["amount_wan"] == "188.00"
    assert any(a["id"] == ach["id"] for a in achs_after)
    assert any(f["id"] == fac["id"] for f in facs_after)


async def test_entity_delete_cleans_up_field_provenance_and_merge_preview_is_readonly(
    client: AsyncClient,
) -> None:
    """Deleting LabProject/LabAchievement/LabFacility removes FieldProvenance rows; merge-preview performs zero DB writes; list_import_jobs defers preview."""
    _, headers = await _seed_admin_and_login(client)

    # 1. Create and delete each entity type -> zero orphan FieldProvenance rows remain
    r_p = await client.post(
        "/api/v1/lab/projects",
        headers=headers,
        json={"title": "待删除课题", "grant_no": "DEL-P-1", "pi_name": "沈昭"},
    )
    pid = uuid.UUID(r_p.json()["project"]["id"])

    r_a = await client.post(
        "/api/v1/lab/achievements",
        headers=headers,
        json={"kind": "paper", "title": "待删除论文", "first_author": "沈昭", "year": 2025},
    )
    aid = uuid.UUID(r_a.json()["achievement"]["id"])

    r_f = await client.post(
        "/api/v1/lab/facilities",
        headers=headers,
        json={"category": "equipment", "name": "待删除仪器", "spec": "X1"},
    )
    fid = uuid.UUID(r_f.json()["facility"]["id"])

    maker = get_sessionmaker()
    async with maker() as session:
        pre_prov = list(
            await session.scalars(
                select(FieldProvenance).where(
                    FieldProvenance.entity_id.in_([pid, aid, fid])
                )
            )
        )
        assert len(pre_prov) >= 6

    assert (
        await client.delete(f"/api/v1/lab/projects/{pid}", headers=headers)
    ).status_code == 200
    assert (
        await client.delete(f"/api/v1/lab/achievements/{aid}", headers=headers)
    ).status_code == 200
    assert (
        await client.delete(f"/api/v1/lab/facilities/{fid}", headers=headers)
    ).status_code == 200

    async with maker() as session:
        orphan_prov = list(
            await session.scalars(
                select(FieldProvenance).where(
                    FieldProvenance.entity_id.in_([pid, aid, fid])
                )
            )
        )
        assert orphan_prov == []

    # 2. Provision a baseline job, wipe FieldProvenance to simulate pre-provenance legacy rows,
    #    then call POST /merge-preview and verify 0 rows are inserted into field_provenance!
    job_base = await _upload_job(
        client,
        headers,
        _build_proposal_docx(
            fill_date_str="2026 年 6 月 1 日",
            wan_title="教授",
            wan_tutor="博士生导师",
        ),
        "base-2026.docx",
        "2026-06-01",
    )
    r_prov = await client.post(
        f"/api/v1/admin/imports/{job_base['job_id']}/provision",
        headers=headers,
        json={"initial_password": INITIAL_PW},
    )
    assert r_prov.status_code == 200, r_prov.text

    job_old = await _upload_job(
        client,
        headers,
        _build_proposal_docx(
            fill_date_str="2023 年 6 月 1 日",
            wan_title="副教授",
            wan_tutor="硕士生导师",
        ),
        "old-2023.docx",
        "2023-06-01",
    )

    async with maker() as session:
        await session.execute(delete(FieldProvenance))
        await session.commit()

    r_mp = await client.post(
        f"/api/v1/admin/imports/{job_old['job_id']}/merge-preview",
        headers=headers,
        json={"as_of_time": "2023-06-01"},
    )
    assert r_mp.status_code == 200, r_mp.text
    mp_data = r_mp.json()
    assert mp_data["summary"]["time_regression"] >= 2

    async with maker() as session:
        prov_after_preview = list(await session.scalars(select(FieldProvenance)))
        assert prov_after_preview == []

    # 3. Verify force_adopt can be undone by re-merging the same job with explicit keep_current,
    #    including coupled project.status + project.status_source returning to inferred!
    projs_initial = (await client.get("/api/v1/lab/projects", headers=headers)).json()[
        "projects"
    ]
    age_initial = next(p for p in projs_initial if p["grant_no"] == "AGE240005")
    assert age_initial["status"] == "completed"
    assert age_initial["status_source"] == "inferred"

    tutor_change = next(
        c
        for c in mp_data["changes"]
        if c["entity_type"] == "person"
        and c["entity_name"] == "万振凯"
        and c["field"] == "tutor_role"
    )
    status_change = next(
        c
        for c in mp_data["changes"]
        if c["entity_type"] == "project"
        and c["field"] == "status"
    )
    r_force = await client.post(
        f"/api/v1/admin/imports/{job_old['job_id']}/merge",
        headers=headers,
        json={
            "decisions": [
                {"change_key": tutor_change["change_key"], "action": "force_adopt"},
                {"change_key": status_change["change_key"], "action": "force_adopt"},
            ]
        },
    )
    assert r_force.status_code == 200, r_force.text
    ppl_forced = (await client.get("/api/v1/lab/people", headers=headers)).json()[
        "people"
    ]
    wan_forced = next(p for p in ppl_forced if p["name"] == "万振凯")
    assert wan_forced["tutor_role"] == "硕士生导师"
    projs_forced = (await client.get("/api/v1/lab/projects", headers=headers)).json()[
        "projects"
    ]
    age_forced = next(p for p in projs_forced if p["grant_no"] == "AGE240005")
    assert age_forced["status"] == "ongoing"
    assert age_forced["status_source"] == "explicit"

    r_unforce = await client.post(
        f"/api/v1/admin/imports/{job_old['job_id']}/merge",
        headers=headers,
        json={
            "decisions": [
                {"change_key": tutor_change["change_key"], "action": "keep_current"},
                {"change_key": status_change["change_key"], "action": "keep_current"},
            ]
        },
    )
    assert r_unforce.status_code == 200, r_unforce.text
    ppl_unforced = (await client.get("/api/v1/lab/people", headers=headers)).json()[
        "people"
    ]
    wan_unforced = next(p for p in ppl_unforced if p["name"] == "万振凯")
    assert wan_unforced["tutor_role"] == "博士生导师"
    projs_unforced = (
        await client.get("/api/v1/lab/projects", headers=headers)
    ).json()["projects"]
    age_unforced = next(p for p in projs_unforced if p["grant_no"] == "AGE240005")
    assert age_unforced["status"] == "completed"
    assert age_unforced["status_source"] == "inferred"

    # 4. Verify DELETE /admin/imports/{job_id} blocks deletion if active FieldProvenance rows still reference job_id
    async with maker() as session:
        j_old_row = await session.get(ImportJob, uuid.UUID(job_old["job_id"]))
        assert j_old_row is not None
        # Force status back to 'parsed' while active FieldProvenance rows remain on wan_unforced
        j_old_row.status = "parsed"
        await session.commit()

    r_del_active_prov = await client.delete(
        f"/api/v1/admin/imports/{job_old['job_id']}",
        headers=headers,
    )
    assert r_del_active_prov.status_code == 409, r_del_active_prov.text
    assert r_del_active_prov.json()["error"]["code"] == "import_already_provisioned"

    # 5. Verify GET /admin/imports defers import_jobs.preview at the SQL level
    captured_sql: list[str] = []
    sync_engine = get_engine().sync_engine

    def _on_cursor(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if "FROM import_jobs" in statement:
            captured_sql.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _on_cursor)
    try:
        r_list = await client.get("/api/v1/admin/imports", headers=headers)
        assert r_list.status_code == 200, r_list.text
        assert len(r_list.json()["imports"]) == 2
    finally:
        event.remove(sync_engine, "before_cursor_execute", _on_cursor)

    assert captured_sql, "Expected SELECT FROM import_jobs"
    for stmt in captured_sql:
        select_clause = stmt.split("FROM import_jobs", 1)[0]
        assert "import_jobs.preview" not in select_clause

    # 6. Verify DB-level ON DELETE SET NULL: deleting ImportJob in SQL detaches Lab* rows instead of CASCADE deleting them
    async with maker() as session:
        j_base_row = await session.get(ImportJob, uuid.UUID(job_base["job_id"]))
        assert j_base_row is not None
        await session.delete(j_base_row)
        await session.commit()

    async with maker() as session:
        people_left = list(await session.scalars(select(LabPerson)))
        projects_left = list(await session.scalars(select(LabProject)))
        achs_left = list(await session.scalars(select(LabAchievement)))
        facs_left = list(await session.scalars(select(LabFacility)))
        assert len(people_left) >= 1
        assert all(p.source_import_id is None for p in people_left)
        assert len(projects_left) >= 1
        assert all(p.source_import_id is None for p in projects_left)
        assert len(achs_left) >= 1
        assert all(a.source_import_id is None for a in achs_left)
        assert all(f.source_import_id is None for f in facs_left)


async def test_revert_preserves_uploaded_signature_person_on_duplicate_name_and_migration_keeps_legacy_imports(
    client: AsyncClient,
) -> None:
    """Reverting a merge preserves an unlinked LabPerson with an uploaded signature even if clearing fields collides with a duplicate-name person's fingerprint; migration 0010 SQL preserves pre-0008 imported assets that only received blank-field manual edits."""
    from datetime import UTC, date, datetime

    from app.importer.fingerprint import person_fp, project_fp
    from sqlalchemy import text

    admin, headers = await _seed_admin_and_login(client)

    job_base = await _upload_job(
        client,
        headers,
        _build_proposal_docx(fill_date_str="2024 年 6 月 1 日", wan_title="副教授"),
        "sig-base-2024.docx",
        "2024-06-01",
    )
    assert (
        await client.post(
            f"/api/v1/admin/imports/{job_base['job_id']}/provision",
            headers=headers,
            json={"initial_password": INITIAL_PW},
        )
    ).status_code == 200

    job_merge = await _upload_job(
        client,
        headers,
        _build_proposal_docx(fill_date_str="2025 年 6 月 1 日", wan_title="教授"),
        "sig-merge-2025.docx",
        "2025-06-01",
    )
    assert (
        await client.post(
            f"/api/v1/admin/imports/{job_merge['job_id']}/merge",
            headers=headers,
            json={},
        )
    ).status_code == 200

    maker = get_sessionmaker()
    base_job_uuid = uuid.UUID(job_base["job_id"])
    merge_job_uuid = uuid.UUID(job_merge["job_id"])

    # 1. Seed two same-name unlinked LabPerson rows ("李重名"):
    #    - existing_dup already holds the cleared fingerprint person_fp(tier="T3", login_name=None, name="李重名", affiliation=None)
    #    - sig_person was created by job_merge with affiliation="天津天狮学院" (from job_merge) AND an uploaded signature (user_id=None)
    cleared_fp = person_fp(
        tier="T3", login_name=None, name="李重名", affiliation=None
    )
    aff_fp = person_fp(
        tier="T3", login_name=None, name="李重名", affiliation="天津天狮学院"
    )
    sig_person_id = uuid.uuid4()
    async with maker() as session:
        existing_dup = LabPerson(
            id=uuid.uuid4(),
            user_id=None,
            entity_fingerprint=cleared_fp,
            name="李重名",
            affiliation=None,
            source_import_id=base_job_uuid,
            as_of_time=date(2024, 6, 1),
        )
        sig_person = LabPerson(
            id=sig_person_id,
            user_id=None,
            entity_fingerprint=aff_fp,
            name="李重名",
            affiliation="天津天狮学院",
            signature_data=b"\x89PNG\r\n\x1a\n",
            signature_mime="image/png",
            signature_sha256="b" * 64,
            signature_updated_at=datetime(2025, 6, 1, tzinfo=UTC),
            signature_source="upload",
            source_import_id=merge_job_uuid,
            as_of_time=date(2025, 6, 1),
        )
        session.add_all([existing_dup, sig_person])
        session.add_all(
            [
                FieldProvenance(
                    entity_type="person",
                    entity_id=sig_person_id,
                    field="name",
                    value="李重名",
                    source_kind="import",
                    source_job_id=merge_job_uuid,
                    as_of_time=date(2025, 6, 1),
                    recorded_by=admin.id,
                    superseded=False,
                ),
                FieldProvenance(
                    entity_type="person",
                    entity_id=sig_person_id,
                    field="affiliation",
                    value="天津天狮学院",
                    source_kind="import",
                    source_job_id=merge_job_uuid,
                    as_of_time=date(2025, 6, 1),
                    recorded_by=admin.id,
                    superseded=False,
                ),
            ]
        )
        await session.commit()

    # Reverting job_merge clears affiliation on sig_person -> would collide with existing_dup's cleared_fp,
    # so sig_person must keep its non-colliding fingerprint, preserve its uploaded signature, and detach source_import_id=None.
    r_rev = await client.post(
        f"/api/v1/admin/imports/{job_merge['job_id']}/revert",
        headers=headers,
    )
    assert r_rev.status_code == 200, r_rev.text

    async with maker() as session:
        refreshed_sig = await session.get(LabPerson, sig_person_id)
        assert refreshed_sig is not None
        assert refreshed_sig.signature_source == "upload"
        assert refreshed_sig.source_import_id is None
        assert refreshed_sig.entity_fingerprint == aff_fp

    # 2. Verify migration 0010 detachment predicate preserves pre-0008 legacy imported assets
    #    that only received a manual edit on a previously blank field, while detaching manual-only creations.
    legacy_ach_id: uuid.UUID
    manual_wrong_proj_id = uuid.uuid4()
    async with maker() as session:
        legacy_ach = await session.scalar(
            select(LabAchievement).where(
                LabAchievement.source_import_id == base_job_uuid
            )
        )
        assert legacy_ach is not None
        legacy_ach_id = legacy_ach.id
        # Simulate pre-0008 state: zero FieldProvenance rows initially, then manual edit on blank support_note
        await session.execute(
            delete(FieldProvenance).where(
                FieldProvenance.entity_type == "achievement",
                FieldProvenance.entity_id == legacy_ach_id,
            )
        )
        session.add(
            FieldProvenance(
                entity_type="achievement",
                entity_id=legacy_ach_id,
                field="support_note",
                value="补充基金标注",
                source_kind="manual",
                source_job_id=None,
                as_of_time=date(2026, 1, 1),
                recorded_by=admin.id,
                superseded=False,
            )
        )

        # Simulate pre-0010 manual creation wrongly attached to base_job_uuid
        man_fp = project_fp(
            grant_no="OLD-MAN-01",
            title="旧版手工附着课题",
            funder=None,
        )
        session.add(
            LabProject(
                id=manual_wrong_proj_id,
                entity_fingerprint=man_fp,
                title="旧版手工附着课题",
                grant_no="OLD-MAN-01",
                status="ongoing",
                status_source="explicit",
                raw_text="旧版手工附着课题",
                source_import_id=base_job_uuid,
                as_of_time=date(2026, 1, 1),
            )
        )
        session.add(
            FieldProvenance(
                entity_type="project",
                entity_id=manual_wrong_proj_id,
                field="title",
                value="旧版手工附着课题",
                source_kind="manual",
                source_job_id=None,
                as_of_time=date(2026, 1, 1),
                recorded_by=admin.id,
                superseded=False,
            )
        )
        await session.flush()

        for table_name, entity_type, preview_key, primary_field in (
            ("lab_achievements", "achievement", "achievements", "title"),
            ("lab_projects", "project", "projects", "title"),
        ):
            await session.execute(
                text(
                    f"""
                    UPDATE {table_name} AS t
                    SET source_import_id = NULL
                    WHERE t.source_import_id IS NOT NULL
                      AND (
                        '{entity_type}' <> 'person'
                        AND EXISTS (
                          SELECT 1 FROM field_provenance fp
                          WHERE fp.entity_type = '{entity_type}'
                            AND fp.entity_id = t.id
                            AND fp.field = '{primary_field}'
                            AND fp.source_kind = 'manual'
                            AND fp.source_job_id IS NULL
                            AND fp.superseded = false
                        )
                        AND NOT EXISTS (
                          SELECT 1 FROM field_provenance fp
                          WHERE fp.entity_type = '{entity_type}'
                            AND fp.entity_id = t.id
                            AND fp.source_kind <> 'manual'
                        )
                        AND NOT EXISTS (
                          SELECT 1
                          FROM import_jobs j,
                               jsonb_array_elements(
                                 CASE
                                   WHEN jsonb_typeof(j.preview -> '{preview_key}') = 'array'
                                   THEN j.preview -> '{preview_key}'
                                   ELSE '[]'::jsonb
                                 END
                               ) AS elem
                          WHERE j.id = t.source_import_id
                            AND elem ->> 'fingerprint' = t.entity_fingerprint
                        )
                      );
                    """
                )
            )
        await session.commit()

    async with maker() as session:
        kept_ach = await session.get(LabAchievement, legacy_ach_id)
        detached_proj = await session.get(LabProject, manual_wrong_proj_id)
        assert kept_ach is not None
        assert kept_ach.source_import_id == base_job_uuid
        assert detached_proj is not None
        assert detached_proj.source_import_id is None
