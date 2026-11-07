"""Tests for Phase 2 field_provenance migration, temporal merge arbitration, backfill, manual edit protection, restore-imported, and revert (Spec §10)."""

from __future__ import annotations

import io
from typing import Any

import docx
from app.auth import hash_password
from app.db import get_sessionmaker
from app.models import Document, FieldProvenance, User
from httpx import AsyncClient
from sqlalchemy import delete, select

ADMIN_EMAIL = "admin@lab.local"
ADMIN_PASSWORD = "LabAgent-Test-Password-2026"
INITIAL_PW = "LabAgent@2026"


def build_proposal_docx_variant(
    *,
    fill_date_str: str = "2026 年 6 月 1 日",
    project_title: str = "教育考试专属大模型构建与安全应用研究",
    wan_title: str = "教授",
    wan_duty: str = "",
    wan_tutor: str = "博士生导师",
    wan_specialty: str = "大模型安全与智能评卷",
    guo_title: str = "教授",
    guo_duty: str = "院长",
    guo_specialty: str = "大数据与云计算",
    age240005_status: str = "已结题",
    include_2023_legacy_project: bool = False,
    include_2026_new_project: bool = True,
    ach1_support_note: str = "",
    include_2023_extra_ach: bool = False,
    include_2026_extra_ach: bool = True,
) -> bytes:
    """Build a synthetic proposal `.docx` parameterized by temporal snapshot."""
    doc = docx.Document()

    # Table 0: Cover
    t1 = doc.add_table(rows=5, cols=2)
    t1_rows = [
        ("项 目 名 称", project_title),
        ("申 请 人", "万振凯"),
        ("所 在 单 位", "天津天狮学院"),
        ("通 讯 地 址", "天津市武清区"),
        ("填 表 日 期", fill_date_str),
    ]
    for idx, (k, v) in enumerate(t1_rows):
        t1.rows[idx].cells[0].text = k
        t1.rows[idx].cells[1].text = v

    # Table 1: Team
    t2 = doc.add_table(rows=8, cols=10)
    t2.rows[0].cells[0].text = "申请人基本情况"
    r1_vals = [
        "姓名",
        "万振凯",
        "性别",
        "男",
        "出生年月",
        "197001",
        "专业技术职务",
        wan_title,
        "行政职务",
        wan_duty,
    ]
    for c_idx, val in enumerate(r1_vals):
        t2.rows[1].cells[c_idx].text = val
    r2_vals = [
        "最终学位",
        "博士",
        "研究专长",
        wan_specialty,
        "担任导师",
        wan_tutor,
        "所在单位",
        "天津天狮学院",
        "电子信箱",
        "",
    ]
    for c_idx, val in enumerate(r2_vals):
        t2.rows[2].cells[c_idx].text = val
    r3_vals = ["证件类型", "身份证", "证件号码", "", "联系电话", "", "", "", "", ""]
    for c_idx, val in enumerate(r3_vals):
        t2.rows[3].cells[c_idx].text = val

    headers = [
        "序号",
        "姓名",
        "证件号码",
        "专业技术职务",
        "行政职务",
        "担任导师",
        "研究专长",
        "最终学位",
        "所在单位名称",
        "电子信箱",
    ]
    for c_idx, h in enumerate(headers):
        t2.rows[4].cells[c_idx].text = h

    members = [
        (
            "1",
            "郭建民",
            "",
            guo_title,
            guo_duty,
            "",
            guo_specialty,
            "学士",
            "天津天狮学院",
            "guojianmin@tiangong.edu.cn",
        ),
        (
            "2",
            "单立飞",
            "",
            "副教授",
            "系主任",
            "",
            "计算机技术",
            "硕士",
            "天津天狮学院",
            "",
        ),
        (
            "3",
            "林雪莹",
            "2431101262",
            "无",
            "",
            "",
            "深度学习",
            "学士",
            "天津工业大学",
            "",
        ),
    ]
    for idx, m in enumerate(members, start=5):
        for c_idx, val in enumerate(m):
            t2.rows[idx].cells[c_idx].text = val

    # Table 2: Budget / Facilities
    t3 = doc.add_table(rows=2, cols=5)
    t3.rows[0].cells[0].text = "预算科目"
    t3.rows[0].cells[2].text = "科目名称"
    t3.rows[0].cells[3].text = "金额"
    t3.rows[0].cells[4].text = "计算依据及理由"
    b_row = [
        "直接费用",
        "3",
        "设备费",
        "8.00",
        "用于课题组搭建教育考试专属大模型轻量化微调与推理验证实验环境所需的计算加速设备及配套部件；课题组已有2台图形工作站。",
    ]
    for c_idx, val in enumerate(b_row):
        t3.rows[1].cells[c_idx].text = val

    # Table 3: Research Foundation
    ach_lines = [
        f"（1）万振凯（第一）：《数智融合赋能天津制造业高质量发展的对策建议》，研究报告，天津市科协，2023年。{ach1_support_note}",
    ]
    if include_2023_extra_ach:
        ach_lines.append(
            "（2）单立飞（第一）：《早期考务信息化历史数据挖掘方法》，教育科学，2022年。构建了早期特征工程。"
        )
    if include_2026_extra_ach:
        ach_lines.append(
            f"（{len(ach_lines) + 1}）郭建民（第一）：《多模态评卷大模型推理加速研究》，计算机研究，2026年。提升了评卷吞吐率。"
        )

    proj_lines = [
        f"（1）《基于人工智能的智能评卷系统关键技术研究》（AGE240005），万振凯（主持），全国教育科学规划课题，2022年8月立项，{age240005_status}。",
    ]
    if include_2023_legacy_project:
        proj_lines.append(
            "（2）《高校计算机基础题库建设研究》（2020JG09），单立飞（主持），天津市教改专项，2020年5月立项，已结题。"
        )
    if include_2026_new_project:
        proj_lines.append(
            f"（{len(proj_lines) + 1}）《大模型安全对齐基准测试集构建》（津科智2025-08），郭建民（主持），市级科研专项，2025年1月立项，在研。"
        )

    t4 = doc.add_table(rows=1, cols=1)
    t4.rows[0].cells[0].text = (
        "前期研究基础\n"
        f"1．[学术简历] 万振凯，天津天狮学院{wan_title}，长期从事人工智能与智能评卷系统研究。\n"
        "2．[前期成果]\n"
        + "\n".join(ach_lines)
        + "\n3．[承担项目]\n"
        + "\n".join(proj_lines)
        + "\n4．[与已承担项目的关系] 本课题是在全国教育科学规划课题（AGE240005）基础上向大模型安全对齐方向的深入拓展。"
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def _seed_admin() -> User:
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
        return admin


async def _login_headers(
    client: AsyncClient, identifier: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD
) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": identifier, "password": password},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _upload_proposal(
    client: AsyncClient,
    headers: dict[str, str],
    docx_bytes: bytes,
    filename: str,
    as_of_time: str,
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/admin/imports",
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


async def test_phase2_clockwise_evolution_2023_then_2026(client: AsyncClient) -> None:
    """Scenario 1: Provision 2023 old proposal first, then merge 2026 new proposal."""
    await _seed_admin()
    headers = await _login_headers(client)

    docx_2023 = build_proposal_docx_variant(
        fill_date_str="2023 年 5 月 1 日",
        wan_title="副教授",
        wan_duty="副系主任",
        wan_tutor="硕士生导师",
        wan_specialty="传统图像识别",
        age240005_status="在研",
        include_2023_legacy_project=True,
        include_2026_new_project=False,
        ach1_support_note="早期工业调研支撑。",
        include_2023_extra_ach=True,
        include_2026_extra_ach=False,
    )
    job_2023 = await _upload_proposal(
        client, headers, docx_2023, "proposal-2023.docx", "2023-05-01"
    )
    prov_resp = await client.post(
        f"/api/v1/admin/imports/{job_2023['job_id']}/provision",
        headers=headers,
        json={"as_of_time": "2023-05-01", "initial_password": INITIAL_PW},
    )
    assert prov_resp.status_code == 200, prov_resp.text

    # Now upload 2026 proposal where 万振凯 promoted to 教授 and AGE240005 completed
    docx_2026 = build_proposal_docx_variant(
        fill_date_str="2026 年 6 月 1 日",
        wan_title="教授",
        wan_duty="院长",
        wan_tutor="博士生导师",
        wan_specialty="大模型安全与智能评卷",
        age240005_status="已结题",
        include_2023_legacy_project=False,
        include_2026_new_project=True,
        ach1_support_note="",
        include_2023_extra_ach=False,
        include_2026_extra_ach=True,
    )
    job_2026 = await _upload_proposal(
        client, headers, docx_2026, "proposal-2026.docx", "2026-06-01"
    )

    preview_resp = await client.post(
        f"/api/v1/admin/imports/{job_2026['job_id']}/merge-preview",
        headers=headers,
    )
    assert preview_resp.status_code == 200, preview_resp.text
    mp = preview_resp.json()
    assert mp["as_of_time"] == "2026-06-01"
    assert mp["summary"]["updated"] >= 3
    assert mp["summary"]["added"] >= 2
    assert mp["summary"]["time_regression"] == 0

    merge_resp = await client.post(
        f"/api/v1/admin/imports/{job_2026['job_id']}/merge",
        headers=headers,
        json={"decisions": []},
    )
    assert merge_resp.status_code == 200, merge_resp.text
    assert merge_resp.json()["status"] == "merged"

    # Verify people & projects & achievements reflect 2026 latest state while retaining 2023 cumulative assets
    ppl_resp = await client.get("/api/v1/lab/people", headers=headers)
    assert ppl_resp.status_code == 200
    wan = next(p for p in ppl_resp.json()["people"] if p["name"] == "万振凯")
    assert wan["title"] == "教授"
    assert wan["admin_duty"] == "院长"
    assert wan["tutor_role"] == "博士生导师"
    assert wan["research_specialty"] == "大模型安全与智能评卷"
    assert wan["as_of_time"] == "2026-06-01"
    assert wan["provenance"]["title"]["as_of_time"] == "2026-06-01"

    proj_resp = await client.get("/api/v1/lab/projects", headers=headers)
    projects = proj_resp.json()["projects"]
    age_proj = next(p for p in projects if p["grant_no"] == "AGE240005")
    assert age_proj["status"] == "completed"
    # 2023 legacy project (2020JG09) is NOT deleted ("缺席不等于删除") and 2026 new project (津科智2025-08) is added
    grant_nos = {p["grant_no"] for p in projects}
    assert {"AGE240005", "2020JG09", "津科智2025-08"} <= grant_nos

    # Verify RAG document is updated with 2026 state
    maker = get_sessionmaker()
    async with maker() as session:
        rag_doc = await session.scalar(
            select(Document).where(
                Document.doc_key == f"lab-profile-{job_2026['job_id']}",
                Document.deleted_at.is_(None),
            )
        )
        assert rag_doc is not None
        assert "教授" in rag_doc.content_text
        assert "大模型安全与智能评卷" in rag_doc.content_text


async def test_phase2_counterclockwise_anti_regression_and_force_adopt(
    client: AsyncClient,
) -> None:
    """Scenarios 2 & 3: Provision 2026 new proposal first, then merge 2023 old proposal (prevent time regression, fill blanks, add historical assets, and support force_adopt)."""
    await _seed_admin()
    headers = await _login_headers(client)

    # 1. Provision 2026 proposal first (where wan_duty and ach1_support_note are blank)
    docx_2026 = build_proposal_docx_variant(
        fill_date_str="2026 年 6 月 1 日",
        wan_title="教授",
        wan_duty="",
        wan_tutor="博士生导师",
        wan_specialty="大模型安全与智能评卷",
        age240005_status="已结题",
        include_2023_legacy_project=False,
        include_2026_new_project=True,
        ach1_support_note="",
        include_2023_extra_ach=False,
        include_2026_extra_ach=True,
    )
    job_2026 = await _upload_proposal(
        client, headers, docx_2026, "proposal-2026.docx", "2026-06-01"
    )
    prov_resp = await client.post(
        f"/api/v1/admin/imports/{job_2026['job_id']}/provision",
        headers=headers,
        json={"as_of_time": "2026-06-01", "initial_password": INITIAL_PW},
    )
    assert prov_resp.status_code == 200, prov_resp.text

    # 2. Upload 2023 older proposal (wan_title="副教授", age240005="在研", but wan_duty="系主任" and ach1_support_note non-empty)
    docx_2023 = build_proposal_docx_variant(
        fill_date_str="2023 年 5 月 1 日",
        wan_title="副教授",
        wan_duty="系主任",
        wan_tutor="硕士生导师",
        wan_specialty="传统图像识别",
        age240005_status="在研",
        include_2023_legacy_project=True,
        include_2026_new_project=False,
        ach1_support_note="早期工业调研支撑。",
        include_2023_extra_ach=True,
        include_2026_extra_ach=False,
    )
    job_2023 = await _upload_proposal(
        client, headers, docx_2023, "proposal-2023.docx", "2023-05-01"
    )

    preview_resp = await client.post(
        f"/api/v1/admin/imports/{job_2023['job_id']}/merge-preview",
        headers=headers,
    )
    assert preview_resp.status_code == 200, preview_resp.text
    mp = preview_resp.json()
    assert mp["summary"]["time_regression"] >= 3
    assert mp["summary"]["fill_blank"] >= 2
    assert mp["summary"]["added"] >= 2

    changes = mp["changes"]
    title_reg = next(
        c
        for c in changes
        if c["entity_type"] == "person"
        and c["entity_name"] == "万振凯"
        and c["field"] == "title"
    )
    assert title_reg["category"] == "time_regression"
    assert title_reg["current_value"] == "教授"
    assert title_reg["current_as_of_time"] == "2026-06-01"
    assert title_reg["incoming_value"] == "副教授"
    assert title_reg["incoming_as_of_time"] == "2023-05-01"
    assert title_reg["default_action"] == "keep_current"

    status_reg = next(
        c
        for c in changes
        if c["entity_type"] == "project" and c["field"] == "status"
    )
    assert status_reg["category"] == "time_regression"
    assert status_reg["current_value"] == "completed"
    assert status_reg["incoming_value"] == "ongoing"
    assert status_reg["default_action"] == "keep_current"

    duty_fill = next(
        c
        for c in changes
        if c["entity_type"] == "person"
        and c["entity_name"] == "万振凯"
        and c["field"] == "admin_duty"
    )
    assert duty_fill["category"] == "fill_blank"
    assert duty_fill["incoming_value"] == "系主任"
    assert duty_fill["default_action"] == "adopt_incoming"

    support_fill = next(
        c
        for c in changes
        if c["entity_type"] == "achievement" and c["field"] == "support_note"
    )
    assert support_fill["category"] == "fill_blank"
    assert support_fill["default_action"] == "adopt_incoming"

    # 3. Execute default merge (no force_adopt): verify ZERO time regression while blanks & historical entities are filled
    merge_resp = await client.post(
        f"/api/v1/admin/imports/{job_2023['job_id']}/merge",
        headers=headers,
        json={"decisions": []},
    )
    assert merge_resp.status_code == 200, merge_resp.text

    ppl_resp = await client.get("/api/v1/lab/people", headers=headers)
    wan = next(p for p in ppl_resp.json()["people"] if p["name"] == "万振凯")
    assert wan["title"] == "教授"  # NOT regressed to 副教授!
    assert wan["tutor_role"] == "博士生导师"  # NOT regressed to 硕士生导师!
    assert wan["admin_duty"] == "系主任"  # Filled from 2023!

    proj_resp = await client.get("/api/v1/lab/projects", headers=headers)
    projects = proj_resp.json()["projects"]
    age_proj = next(p for p in projects if p["grant_no"] == "AGE240005")
    assert age_proj["status"] == "completed"  # NOT regressed to ongoing!
    assert any(p["grant_no"] == "2020JG09" for p in projects)  # 2023 project added!

    ach_resp = await client.get("/api/v1/lab/achievements", headers=headers)
    achs = ach_resp.json()["achievements"]
    rep_ach = next(
        a for a in achs if "数智融合赋能天津制造业高质量发展的对策建议" in a["title"]
    )
    assert rep_ach["support_note"] == "早期工业调研支撑。"  # Filled from 2023!
    assert any("早期考务信息化历史数据挖掘方法" in a["title"] for a in achs)

    # 4. Scenario 3: Re-merge with force_adopt on tutor_role time_regression
    tutor_reg = next(
        c
        for c in changes
        if c["entity_type"] == "person"
        and c["entity_name"] == "万振凯"
        and c["field"] == "tutor_role"
    )
    force_resp = await client.post(
        f"/api/v1/admin/imports/{job_2023['job_id']}/merge",
        headers=headers,
        json={
            "decisions": [
                {"change_key": tutor_reg["change_key"], "action": "force_adopt"}
            ]
        },
    )
    assert force_resp.status_code == 200, force_resp.text
    assert force_resp.json()["stats"]["forced_fields"] == 1

    ppl_after_force = await client.get("/api/v1/lab/people", headers=headers)
    wan_forced = next(
        p for p in ppl_after_force.json()["people"] if p["name"] == "万振凯"
    )
    assert wan_forced["tutor_role"] == "硕士生导师"
    assert wan_forced["provenance"]["tutor_role"]["source_kind"] == "manual"
    assert wan_forced["provenance"]["tutor_role"]["has_manual_override"] is True


async def test_phase2_manual_edit_protection_and_restore_imported(
    client: AsyncClient,
) -> None:
    """Scenario 4: Manual PATCH creates source_kind='manual', protects against 2027 import, and restore-imported restores to latest 2027 imported value."""
    await _seed_admin()
    headers = await _login_headers(client)

    docx_2026 = build_proposal_docx_variant(
        fill_date_str="2026 年 6 月 1 日",
        wan_specialty="大模型安全",
    )
    job_2026 = await _upload_proposal(
        client, headers, docx_2026, "proposal-2026.docx", "2026-06-01"
    )
    await client.post(
        f"/api/v1/admin/imports/{job_2026['job_id']}/provision",
        headers=headers,
        json={"as_of_time": "2026-06-01", "initial_password": INITIAL_PW},
    )

    ppl_resp = await client.get("/api/v1/lab/people", headers=headers)
    wan = next(p for p in ppl_resp.json()["people"] if p["name"] == "万振凯")
    wan_id = wan["id"]

    # 1. Manually edit research_specialty via PATCH
    patch_resp = await client.patch(
        f"/api/v1/lab/people/{wan_id}",
        headers=headers,
        json={"research_specialty": "手工精修：隐私计算与联邦学习"},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    patched_person = patch_resp.json()["person"]
    assert patched_person["research_specialty"] == "手工精修：隐私计算与联邦学习"
    spec_prov = patched_person["provenance"]["research_specialty"]
    assert spec_prov["source_kind"] == "manual"
    assert spec_prov["has_manual_override"] is True
    assert spec_prov["imported_value"] == "大模型安全"

    # 2. Upload & preview a newer 2027 proposal with a different research_specialty
    docx_2027 = build_proposal_docx_variant(
        fill_date_str="2027 年 9 月 1 日",
        wan_specialty="具身智能与教育机器人",
    )
    job_2027 = await _upload_proposal(
        client, headers, docx_2027, "proposal-2027.docx", "2027-09-01"
    )

    mp_resp = await client.post(
        f"/api/v1/admin/imports/{job_2027['job_id']}/merge-preview",
        headers=headers,
    )
    assert mp_resp.status_code == 200, mp_resp.text
    mp = mp_resp.json()
    assert mp["summary"]["manual_protected"] >= 1
    spec_change = next(
        c
        for c in mp["changes"]
        if c["entity_id"] == wan_id and c["field"] == "research_specialty"
    )
    assert spec_change["category"] == "manual_protected"
    assert spec_change["default_action"] == "keep_current"

    # 3. Merge 2027 proposal with default decisions: manual edit must NOT be overwritten
    merge_resp = await client.post(
        f"/api/v1/admin/imports/{job_2027['job_id']}/merge",
        headers=headers,
        json={"decisions": []},
    )
    assert merge_resp.status_code == 200, merge_resp.text

    ppl_after_merge = await client.get("/api/v1/lab/people", headers=headers)
    wan_after = next(
        p for p in ppl_after_merge.json()["people"] if p["id"] == wan_id
    )
    assert wan_after["research_specialty"] == "手工精修：隐私计算与联邦学习"
    assert (
        wan_after["provenance"]["research_specialty"]["imported_value"]
        == "具身智能与教育机器人"
    )

    # 4. Call restore-imported: manual entry becomes superseded=True and value restores to 2027 import
    restore_resp = await client.post(
        f"/api/v1/lab/people/{wan_id}/restore-imported",
        headers=headers,
        json={"field": "research_specialty"},
    )
    assert restore_resp.status_code == 200, restore_resp.text
    restored_person = restore_resp.json()["person"]
    assert (
        restored_person["research_specialty"]
        == "具身智能与教育机器人"
    )
    assert (
        restored_person["provenance"]["research_specialty"]["source_kind"]
        == "import"
    )
    assert (
        restored_person["provenance"]["research_specialty"]["has_manual_override"]
        is False
    )
    assert (
        restored_person["provenance"]["research_specialty"]["as_of_time"]
        == "2027-09-01"
    )

    # 5. Regression check: PATCH facility.spec then restore-imported recomputes entity_fingerprint
    fac_resp = await client.get("/api/v1/lab/facilities", headers=headers)
    fac = fac_resp.json()["facilities"][0]
    fac_id = fac["id"]
    orig_fac_fp = fac["entity_fingerprint"]
    orig_fac_spec = fac["spec"]

    patch_fac = await client.patch(
        f"/api/v1/lab/facilities/{fac_id}",
        headers=headers,
        json={"spec": "8xH100 临时手工规格"},
    )
    assert patch_fac.status_code == 200, patch_fac.text
    assert patch_fac.json()["facility"]["entity_fingerprint"] != orig_fac_fp

    restore_fac = await client.post(
        f"/api/v1/lab/facilities/{fac_id}/restore-imported",
        headers=headers,
        json={"field": "spec"},
    )
    assert restore_fac.status_code == 200, restore_fac.text
    assert restore_fac.json()["facility"]["spec"] == orig_fac_spec
    assert restore_fac.json()["facility"]["entity_fingerprint"] == orig_fac_fp

    # 6. Regression check: non-admin member cannot restore project_role via restore-imported
    admin_role_patch = await client.patch(
        f"/api/v1/lab/people/{wan_id}",
        headers=headers,
        json={"project_role": "member"},
    )
    assert admin_role_patch.status_code == 200
    member_headers = await _login_headers(client, identifier="wanzhenkai", password=INITIAL_PW)
    new_member_pw = "WanZhenKai-NewPass-2026#9"
    chg_resp = await client.post(
        "/api/v1/auth/change-password",
        headers=member_headers,
        json={"old_password": INITIAL_PW, "new_password": new_member_pw},
    )
    assert chg_resp.status_code == 200, chg_resp.text
    member_headers = await _login_headers(client, identifier="wanzhenkai", password=new_member_pw)
    forbid_restore = await client.post(
        f"/api/v1/lab/people/{wan_id}/restore-imported",
        headers=member_headers,
        json={"field": "project_role"},
    )
    assert forbid_restore.status_code == 403
    assert forbid_restore.json()["error"]["code"] == "forbidden"


async def test_phase2_same_time_conflict_resolution(client: AsyncClient) -> None:
    """Scenario 5: Two proposals with identical as_of_time but different field values require explicit decision."""
    await _seed_admin()
    headers = await _login_headers(client)

    docx_a = build_proposal_docx_variant(
        fill_date_str="2026 年 6 月 1 日",
        project_title="同时间本子A",
        wan_title="副教授",
        wan_duty="系主任",
    )
    job_a = await _upload_proposal(
        client, headers, docx_a, "proposal-2026-a.docx", "2026-06-01"
    )
    await client.post(
        f"/api/v1/admin/imports/{job_a['job_id']}/provision",
        headers=headers,
        json={"as_of_time": "2026-06-01", "initial_password": INITIAL_PW},
    )

    docx_b = build_proposal_docx_variant(
        fill_date_str="2026 年 6 月 1 日",
        project_title="同时间本子B",
        wan_title="教授",
        wan_duty="副院长",
    )
    job_b = await _upload_proposal(
        client, headers, docx_b, "proposal-2026-b.docx", "2026-06-01"
    )

    mp_resp = await client.post(
        f"/api/v1/admin/imports/{job_b['job_id']}/merge-preview",
        headers=headers,
    )
    assert mp_resp.status_code == 200
    mp = mp_resp.json()
    assert mp["summary"]["same_time_conflict"] >= 1
    conflict_items = [
        c for c in mp["changes"] if c["category"] == "same_time_conflict"
    ]
    assert all(c["requires_decision"] is True for c in conflict_items)

    # Merging without resolving same_time_conflict must fail with 422 merge_conflict_unresolved
    bad_merge = await client.post(
        f"/api/v1/admin/imports/{job_b['job_id']}/merge",
        headers=headers,
        json={"decisions": []},
    )
    assert bad_merge.status_code == 422
    assert bad_merge.json()["error"]["code"] == "merge_conflict_unresolved"

    # Provide explicit decisions for all same_time_conflict items (title -> adopt_incoming, admin_duty -> keep_current)
    decisions = [
        {
            "change_key": c["change_key"],
            "action": "adopt_incoming" if c["field"] == "title" else "keep_current",
        }
        for c in conflict_items
    ]
    ok_merge = await client.post(
        f"/api/v1/admin/imports/{job_b['job_id']}/merge",
        headers=headers,
        json={"decisions": decisions},
    )
    assert ok_merge.status_code == 200, ok_merge.text

    ppl_resp = await client.get("/api/v1/lab/people", headers=headers)
    wan = next(p for p in ppl_resp.json()["people"] if p["name"] == "万振凯")
    assert wan["title"] == "教授"
    assert wan["admin_duty"] == "系主任"
    assert wan["provenance"]["title"]["source_kind"] == "import"
    assert wan["provenance"]["title"]["has_manual_override"] is False

    # Regression check: backfill-provenance must NOT resurrect the superseded keep_current row (admin_duty="副院长")
    bf_after_keep = await client.post(
        "/api/v1/admin/imports/backfill-provenance",
        headers=headers,
    )
    assert bf_after_keep.status_code == 200, bf_after_keep.text
    assert bf_after_keep.json()["discrepancies"] == []

    # A strictly newer 2027 proposal must classify title as 'updated' (NOT 'manual_protected')
    docx_c = build_proposal_docx_variant(
        fill_date_str="2027 年 6 月 1 日",
        project_title="后续2027本子",
        wan_title="讲席教授",
    )
    job_c = await _upload_proposal(
        client, headers, docx_c, "proposal-2027-c.docx", "2027-06-01"
    )
    # Regression check: patch job_c preview so a project has the same title/funder as AGE240005 but a distinct grant_no AGE999999
    c_projects = list(job_c["preview"]["projects"])
    c_projects[0] = {**c_projects[0], "grant_no": "AGE999999", "fingerprint": ""}
    patch_prev = await client.patch(
        f"/api/v1/admin/imports/{job_c['job_id']}/preview",
        headers=headers,
        json={"projects": c_projects},
    )
    assert patch_prev.status_code == 200, patch_prev.text

    mp_c_resp = await client.post(
        f"/api/v1/admin/imports/{job_c['job_id']}/merge-preview",
        headers=headers,
    )
    assert mp_c_resp.status_code == 200
    mp_c = mp_c_resp.json()
    title_change_c = next(
        c
        for c in mp_c["changes"]
        if c["entity_type"] == "person"
        and c["entity_name"] == "万振凯"
        and c["field"] == "title"
    )
    assert title_change_c["category"] == "updated"
    assert title_change_c["default_action"] == "adopt_incoming"
    # Distinct grant_no with same title must be added as a new project, not matched to AGE240005
    assert any(
        e["entity_type"] == "project"
        for e in mp_c["added_entities"]
    )
    assert not any(
        c["entity_type"] == "project" and c["field"] == "grant_no"
        for c in mp_c["changes"]
    )


async def test_phase2_revert_merge_and_backfill_provenance(
    client: AsyncClient,
) -> None:
    """Scenarios 6 & 7: One-click revert rolls back merged updates & added entities without affecting user accounts; backfill-provenance is idempotent with zero discrepancies."""
    await _seed_admin()
    headers = await _login_headers(client)

    docx_2023 = build_proposal_docx_variant(
        fill_date_str="2023 年 5 月 1 日",
        wan_title="副教授",
        age240005_status="在研",
        include_2023_legacy_project=True,
        include_2026_new_project=False,
        include_2023_extra_ach=True,
        include_2026_extra_ach=False,
    )
    job_2023 = await _upload_proposal(
        client, headers, docx_2023, "proposal-2023.docx", "2023-05-01"
    )
    await client.post(
        f"/api/v1/admin/imports/{job_2023['job_id']}/provision",
        headers=headers,
        json={"as_of_time": "2023-05-01", "initial_password": INITIAL_PW},
    )

    # Test Scenario 7: Delete existing provenance rows to simulate pre-Phase-2 legacy state, then run backfill twice
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(delete(FieldProvenance))
        await session.commit()

    # Regression check: calling restore-imported on a legacy record with no provenance rows must preserve current value and establish baseline
    ppl_pre_bf = await client.get("/api/v1/lab/people", headers=headers)
    wan_legacy = next(
        p for p in ppl_pre_bf.json()["people"] if p["name"] == "万振凯"
    )
    legacy_restore = await client.post(
        f"/api/v1/lab/people/{wan_legacy['id']}/restore-imported",
        headers=headers,
        json={"field": "title"},
    )
    assert legacy_restore.status_code == 200, legacy_restore.text
    assert legacy_restore.json()["person"]["title"] == "副教授"

    bf1 = await client.post(
        "/api/v1/admin/imports/backfill-provenance", headers=headers
    )
    assert bf1.status_code == 200, bf1.text
    bf1_data = bf1.json()
    assert bf1_data["backfilled_entries"] > 0
    assert bf1_data["discrepancies"] == []

    bf2 = await client.post(
        "/api/v1/admin/imports/backfill-provenance", headers=headers
    )
    assert bf2.status_code == 200, bf2.text
    bf2_data = bf2.json()
    assert bf2_data["backfilled_entries"] == bf1_data["backfilled_entries"]
    assert bf2_data["discrepancies"] == []

    # Merge 2026 proposal on top of 2023
    docx_2026 = build_proposal_docx_variant(
        fill_date_str="2026 年 6 月 1 日",
        wan_title="教授",
        age240005_status="已结题",
        include_2023_legacy_project=False,
        include_2026_new_project=True,
        include_2023_extra_ach=False,
        include_2026_extra_ach=True,
    )
    job_2026 = await _upload_proposal(
        client, headers, docx_2026, "proposal-2026.docx", "2026-06-01"
    )
    merge_resp = await client.post(
        f"/api/v1/admin/imports/{job_2026['job_id']}/merge",
        headers=headers,
        json={"decisions": []},
    )
    assert merge_resp.status_code == 200

    # Verify 2026 state took effect before revert
    ppl_mid = await client.get("/api/v1/lab/people", headers=headers)
    assert (
        next(p for p in ppl_mid.json()["people"] if p["name"] == "万振凯")["title"]
        == "教授"
    )
    proj_mid = await client.get("/api/v1/lab/projects", headers=headers)
    assert any(p["grant_no"] == "津科智2025-08" for p in proj_mid.json()["projects"])

    # Count User accounts before revert
    async with maker() as session:
        users_before = {
            (u.id, u.login_name, u.role) for u in await session.scalars(select(User))
        }

    # Test Scenario 6: Revert the 2026 merge
    revert_resp = await client.post(
        f"/api/v1/admin/imports/{job_2026['job_id']}/revert",
        headers=headers,
    )
    assert revert_resp.status_code == 200, revert_resp.text
    rev_data = revert_resp.json()
    assert rev_data["job_status"] == "parsed"
    assert rev_data["reverted_fields"] >= 2
    assert rev_data["deleted_entities"] >= 2

    # Verify fields rolled back to 2023 values and 2026-only project/achievement were deleted
    ppl_after = await client.get("/api/v1/lab/people", headers=headers)
    wan_after = next(
        p for p in ppl_after.json()["people"] if p["name"] == "万振凯"
    )
    assert wan_after["title"] == "副教授"
    assert wan_after["as_of_time"] == "2023-05-01"

    proj_after = await client.get("/api/v1/lab/projects", headers=headers)
    projects_after = proj_after.json()["projects"]
    age_after = next(p for p in projects_after if p["grant_no"] == "AGE240005")
    assert age_after["status"] == "ongoing"
    assert not any(p["grant_no"] == "津科智2025-08" for p in projects_after)

    # Verify User accounts are 100% untouched
    async with maker() as session:
        users_after = {
            (u.id, u.login_name, u.role) for u in await session.scalars(select(User))
        }
        assert users_after == users_before
