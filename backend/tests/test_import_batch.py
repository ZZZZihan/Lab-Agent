"""End-to-end tests for Batch Proposal Cold-Start Identity Resolution & Provisioning."""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime

import docx
from app.auth import hash_password
from app.config import get_settings
from app.db import get_sessionmaker
from app.importer.batch_identity import (
    _extract_native_student_id,
    _is_person_excluded,
)
from app.models import ImportJob, LabAchievement, LabPerson, LabProject, User
from httpx import AsyncClient
from sqlalchemy import select
from tests.lab_v22_helpers import make_png_bytes

ADMIN_EMAIL = "admin@lab.local"
ADMIN_PASSWORD = "LabAgent-Test-Password-2026"
INITIAL_PW = "BatchColdStart-2026!"

_ID18_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID18_CHECK_CHARS = "10X98765432"


def _make_synthetic_id18(prefix17: str) -> str:
    assert len(prefix17) == 17 and prefix17.isdigit()
    total = sum(int(ch) * w for ch, w in zip(prefix17, _ID18_WEIGHTS, strict=True))
    return prefix17 + _ID18_CHECK_CHARS[total % 11]


SYNTH_ID_1 = _make_synthetic_id18("11010119900101101")
SYNTH_ID_2 = _make_synthetic_id18("12010119920202202")


async def _seed_admin(
    *,
    display_name: str = "沈昭",
    login_name: str | None = None,
) -> User:
    maker = get_sessionmaker()
    async with maker() as session:
        admin = User(
            email=ADMIN_EMAIL,
            login_name=login_name,
            display_name=display_name,
            role="admin",
            password_hash=hash_password(ADMIN_PASSWORD),
            is_active=True,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        return admin


async def _login_headers(
    client: AsyncClient,
    identifier: str = ADMIN_EMAIL,
    password: str = ADMIN_PASSWORD,
) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": identifier, "password": password},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _build_proposal_docx(
    *,
    project_title: str,
    applicant_name: str,
    applicant_title: str = "教授",
    applicant_affiliation: str = "天津工业大学",
    applicant_email: str = "",
    fill_date: str = "2024年3月15日",
    members: list[tuple[str, str, str, str, str]],
    foundation_text: str | None = None,
    member_signatures: dict[int, bytes] | None = None,
) -> bytes:
    """Build a synthetic proposal .docx with configurable applicant, members, and optional signature images.

    Each member tuple is `(name, cert, title, affiliation, email)`.
    """
    doc = docx.Document()

    t1 = doc.add_table(rows=4, cols=2)
    t1.cell(0, 0).text = "项目名称"
    t1.cell(0, 1).text = project_title
    t1.cell(1, 0).text = "申请人"
    t1.cell(1, 1).text = applicant_name
    t1.cell(2, 0).text = "所在单位"
    t1.cell(2, 1).text = applicant_affiliation
    t1.cell(3, 0).text = "填表日期"
    t1.cell(3, 1).text = fill_date

    has_sigs = bool(member_signatures)
    cols = 7 if has_sigs else 6
    t2 = doc.add_table(rows=3 + len(members), cols=cols)
    t2.cell(0, 0).text = "申请人姓名"
    t2.cell(0, 1).text = applicant_name
    t2.cell(0, 2).text = "专业技术职务"
    t2.cell(0, 3).text = applicant_title
    t2.cell(0, 4).text = "所在单位"
    t2.cell(0, 5).text = applicant_affiliation

    t2.cell(1, 0).text = "最终学位"
    t2.cell(1, 1).text = "博士"
    t2.cell(1, 2).text = "研究专长"
    t2.cell(1, 3).text = "人工智能"
    t2.cell(1, 4).text = "电子信箱"
    t2.cell(1, 5).text = applicant_email

    headers = ["序号", "姓名", "证件号码", "专业技术职务", "所在单位", "电子信箱"]
    if has_sigs:
        headers.append("本人签字")
    for c_idx, h in enumerate(headers):
        t2.cell(2, c_idx).text = h

    for idx, (m_name, m_cert, m_title, m_aff, m_email) in enumerate(members):
        r_idx = 3 + idx
        t2.cell(r_idx, 0).text = str(idx + 1)
        t2.cell(r_idx, 1).text = m_name
        t2.cell(r_idx, 2).text = m_cert
        t2.cell(r_idx, 3).text = m_title
        t2.cell(r_idx, 4).text = m_aff
        t2.cell(r_idx, 5).text = m_email
        if has_sigs and member_signatures and idx in member_signatures:
            para = t2.cell(r_idx, 6).paragraphs[0]
            para.add_run().add_picture(io.BytesIO(member_signatures[idx]))

    t4 = doc.add_table(rows=1, cols=1)
    t4.cell(0, 0).text = foundation_text or (
        "前期研究基础\n"
        f"1．[学术简历] {applicant_name}，长期从事人工智能与大模型系统研究。\n"
        "2．[前期成果]\n"
        f"（1）{applicant_name}（第一）：《{project_title}前期成果》，中国科学，2024年。\n"
        "3．[承担项目]\n"
        f"（1）《{project_title}先导课题》（AGE240005），{applicant_name}（主持），全国教育科学规划课题，2024年1月立项，在研。\n"
    )

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def test_batch_resolve_and_provision_sibling_t1_enrichment_and_idempotency(
    client: AsyncClient,
) -> None:
    """Test 1: 3 proposals where 2024 has 张三 without student ID (T2) and 2025 has 张三 with T1 ID 2431101262."""
    await _seed_admin()
    headers = await _login_headers(client)

    # Proposal A (2024): 张三 has 18-digit ID card (dropped -> T2), 李四 has no email
    docx_2024 = _build_proposal_docx(
        project_title="2024申报课题A",
        applicant_name="王教授",
        fill_date="2024年3月10日",
        members=[
            ("张三", SYNTH_ID_1, "硕士生", "天津工业大学", ""),
            ("李四", SYNTH_ID_2, "讲师", "天津工业大学", ""),
        ],
    )
    # Proposal B (2025): 张三 has student ID 2431101262 (T1), 李四 has email lisi@tiangong.edu.cn (T3)
    docx_2025 = _build_proposal_docx(
        project_title="2025申报课题B",
        applicant_name="王教授",
        fill_date="2025年4月20日",
        members=[
            ("张三", "2431101262", "硕士生", "天津工业大学", ""),
            ("李四", "", "副教授", "天津工业大学", "lisi@tiangong.edu.cn"),
        ],
    )
    # Proposal C (2026): 张三 has no student ID, plus new student 赵六 with T1 ID 2531109988
    docx_2026 = _build_proposal_docx(
        project_title="2026申报课题C",
        applicant_name="王教授",
        fill_date="2026年5月15日",
        members=[
            ("张三", "", "博士生", "天津工业大学", ""),
            ("赵六", "2531109988", "硕士生", "天津工业大学", ""),
        ],
    )

    # Upload out of chronological order: 2026, 2024, 2025
    r_c = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("prop_2026.docx", docx_2026, "application/octet-stream")},
        headers=headers,
    )
    r_a = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("prop_2024.docx", docx_2024, "application/octet-stream")},
        headers=headers,
    )
    r_b = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("prop_2025.docx", docx_2025, "application/octet-stream")},
        headers=headers,
    )
    assert r_a.status_code == 200 and r_b.status_code == 200 and r_c.status_code == 200
    job_a_id = r_a.json()["job_id"]
    job_b_id = r_b.json()["job_id"]
    job_c_id = r_c.json()["job_id"]

    # Before batch/resolve, Proposal A's 张三 is T2 (zhangsan)
    p_a_before = next(
        p for p in r_a.json()["preview"]["people"] if p["name"] == "张三"
    )
    assert p_a_before["tier"] == "T2"
    assert p_a_before["login_name"] == "zhangsan"

    # Call batch/resolve
    res1 = await client.post(
        "/api/v1/admin/imports/batch/resolve",
        json={"job_ids": [job_c_id, job_a_id, job_b_id]},
        headers=headers,
    )
    assert res1.status_code == 200, res1.text
    data1 = res1.json()

    # Verify quality metrics: 4 unique people (王教授, 张三, 李四, 赵六), 2 T1 covered (张三, 赵六)
    assert data1["quality"]["people_total"] == 4
    assert data1["quality"]["t1_covered"] == 2
    assert data1["quality"]["t1_ratio"] == 0.5
    assert data1["quality"]["injected_student_ids"] == 2  # injected into 2024 and 2026
    assert data1["quality"]["injected_emails"] == 1  # injected into 2024 for 李四

    jobs_by_id = {j["job_id"]: j for j in data1["jobs"]}
    p_a_after = next(
        p for p in jobs_by_id[job_a_id]["preview"]["people"] if p["name"] == "张三"
    )
    assert p_a_after["tier"] == "T1"
    assert p_a_after["login_name"] == "2431101262"
    assert p_a_after["student_id"] == "2431101262"
    assert p_a_after["student_id_source"] == "prop_2025.docx"
    assert "STUDENT_ID_FROM_SIBLING" in p_a_after["warnings"]

    lisi_a_after = next(
        p for p in jobs_by_id[job_a_id]["preview"]["people"] if p["name"] == "李四"
    )
    assert lisi_a_after["tier"] == "T3"
    assert lisi_a_after["email"] == "lisi@tiangong.edu.cn"
    assert lisi_a_after["email_source"] == "prop_2025.docx"
    assert "EMAIL_FROM_SIBLING" in lisi_a_after["warnings"]

    # Call batch/resolve a second time -> 100% idempotent
    res2 = await client.post(
        "/api/v1/admin/imports/batch/resolve",
        json={"job_ids": [job_c_id, job_a_id, job_b_id]},
        headers=headers,
    )
    assert res2.status_code == 200, res2.text
    data2 = res2.json()
    assert data2["quality"] == data1["quality"]
    assert data2["clusters"] == data1["clusters"]
    for jid in (job_a_id, job_b_id, job_c_id):
        ppl1 = {p["key"]: p for p in jobs_by_id[jid]["preview"]["people"]}
        ppl2 = {
            p["key"]: p
            for p in next(j for j in data2["jobs"] if j["job_id"] == jid)["preview"]["people"]
        }
        assert ppl1 == ppl2

    # Provision the 3 jobs in batch (submitted as [C(2026), A(2024), B(2025)])
    prov_resp = await client.post(
        "/api/v1/admin/imports/batch/provision",
        json={
            "job_ids": [job_c_id, job_a_id, job_b_id],
            "initial_password": INITIAL_PW,
            "include_password": True,
        },
        headers=headers,
    )
    assert prov_resp.status_code == 200, prov_resp.text
    prov_data = prov_resp.json()

    # Chronological order must be A (2024) -> B (2025) -> C (2026)
    assert [j["job_id"] for j in prov_data["jobs"]] == [
        job_a_id,
        job_b_id,
        job_c_id,
    ]
    assert prov_data["merge_queue"] == [job_b_id, job_c_id]
    assert prov_data["rag_document_id"] is not None
    assert prov_data["rag_doc_key"] == f"lab-profile-{job_c_id}"

    # Combined roster must have 张三 only ONCE under new accounts and NOT under existing accounts
    roster_text = prov_data["roster_text"]
    assert roster_text.count("2431101262") == 1
    assert "以下成员已有账号" not in roster_text

    roster_csv = prov_data["roster_csv"]
    assert roster_csv.count("2431101262") == 1
    assert "已有" not in roster_csv

    # Verify in DB: exactly 1 User and 1 LabPerson for 张三 with login_name="2431101262"
    maker = get_sessionmaker()
    async with maker() as session:
        zs_users = list(
            await session.scalars(select(User).where(User.display_name == "张三"))
        )
        assert len(zs_users) == 1
        assert zs_users[0].login_name == "2431101262"
        assert zs_users[0].role == "member"

        zs_people = list(
            await session.scalars(select(LabPerson).where(LabPerson.name == "张三"))
        )
        assert len(zs_people) == 1
        assert zs_people[0].user_id == zs_users[0].id


async def test_suffixed_pinyin_login_never_mistaken_for_t1_student_id(
    client: AsyncClient,
) -> None:
    """Test 2: Suffixed pinyin logins like zhangsan2 are never mistaken for T1 student IDs."""
    # 1. Direct unit checks against _extract_native_student_id
    assert (
        _extract_native_student_id(
            {
                "name": "张三",
                "tier": "T2",
                "login_name": "zhangsan2",
                "warnings": ["LOGIN_NAME_SUFFIXED"],
            }
        )
        is None
    )
    # Even if tier="T1" or student_id="zhangsan2" is passed, suffixed pinyin guard rejects it
    assert (
        _extract_native_student_id(
            {
                "name": "张三",
                "tier": "T1",
                "login_name": "zhangsan2",
                "student_id": "zhangsan2",
                "warnings": [],
            }
        )
        is None
    )
    # Valid T1 student ID is accepted
    assert (
        _extract_native_student_id(
            {
                "name": "张三",
                "tier": "T1",
                "login_name": "2431101262",
                "student_id": "2431101262",
                "warnings": [],
            }
        )
        == "2431101262"
    )

    # 2. End-to-end check: Proposal 1 has two 张三 (different affiliations -> zhangsan & zhangsan2),
    # Proposal 2 has one 张三 -> neither gets zhangsan2 injected as a T1 student_id
    await _seed_admin()
    headers = await _login_headers(client)

    docx_1 = _build_proposal_docx(
        project_title="重名后缀测试A",
        applicant_name="张三",
        applicant_affiliation="天津工业大学",
        fill_date="2024年6月1日",
        members=[
            ("张三", "", "副教授", "南开大学", ""),
        ],
    )
    docx_2 = _build_proposal_docx(
        project_title="重名后缀测试B",
        applicant_name="李教授",
        applicant_affiliation="天津工业大学",
        fill_date="2025年6月1日",
        members=[
            ("张三", "", "教授", "天津工业大学", ""),
        ],
    )
    r1 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("s1.docx", docx_1, "application/octet-stream")},
        headers=headers,
    )
    r2 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("s2.docx", docx_2, "application/octet-stream")},
        headers=headers,
    )
    j1_id = r1.json()["job_id"]
    j2_id = r2.json()["job_id"]

    # Confirm s1 generated zhangsan2
    s1_logins = [p["login_name"] for p in r1.json()["preview"]["people"]]
    assert "zhangsan2" in s1_logins

    res = await client.post(
        "/api/v1/admin/imports/batch/resolve",
        json={"job_ids": [j1_id, j2_id]},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    q = res.json()["quality"]
    assert q["t1_covered"] == 0
    assert q["injected_student_ids"] == 0
    for job_item in res.json()["jobs"]:
        for p in job_item["preview"]["people"]:
            assert p["tier"] != "T1"
            assert p.get("student_id") is None


async def test_excluded_people_skipped_in_clustering_and_quality(
    client: AsyncClient,
) -> None:
    """Test 3: Excluded people are skipped in pre-pass counting, clustering, and quality metrics."""
    await _seed_admin()
    headers = await _login_headers(client)

    assert _is_person_excluded({"key": "p1", "exclude": True}) is True
    assert _is_person_excluded({"key": "p1", "excluded": True}) is True
    assert _is_person_excluded({"key": "p1", "include": False}) is True
    assert _is_person_excluded({"key": "p1", "imported": False}) is True
    assert _is_person_excluded({"key": "p1", "override_mode": "exclude"}) is True
    assert _is_person_excluded({"key": "p1"}, {"p1"}) is True
    assert _is_person_excluded({"key": "p1", "exclude": False}) is False

    docx_a = _build_proposal_docx(
        project_title="排除测试课题A",
        applicant_name="王教授",
        fill_date="2024年5月1日",
        members=[
            ("张三", "2431101262", "硕士生", "天津工业大学", ""),
            ("被排除人", "2431109999", "硕士生", "天津工业大学", ""),
        ],
    )
    docx_b = _build_proposal_docx(
        project_title="排除测试课题B",
        applicant_name="王教授",
        fill_date="2025年5月1日",
        members=[
            ("张三", "", "硕士生", "天津工业大学", ""),
            ("被排除人", "", "硕士生", "天津工业大学", ""),
        ],
    )
    r_a = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("ex_a.docx", docx_a, "application/octet-stream")},
        headers=headers,
    )
    r_b = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("ex_b.docx", docx_b, "application/octet-stream")},
        headers=headers,
    )
    ja_id = r_a.json()["job_id"]
    jb_id = r_b.json()["job_id"]

    # Mark "被排除人" as exclude=True in both jobs via PATCH preview
    for jid, r_data in ((ja_id, r_a.json()), (jb_id, r_b.json())):
        ppl = r_data["preview"]["people"]
        for p in ppl:
            if p["name"] == "被排除人":
                p["exclude"] = True
        patch_resp = await client.patch(
            f"/api/v1/admin/imports/{jid}/preview",
            json={"people": ppl},
            headers=headers,
        )
        assert patch_resp.status_code == 200

    res = await client.post(
        "/api/v1/admin/imports/batch/resolve",
        json={"job_ids": [ja_id, jb_id]},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()
    # Only 王教授 and 张三 should be in clusters & quality
    assert data["quality"]["people_total"] == 2
    assert data["quality"]["t1_covered"] == 1
    assert data["quality"]["t1_ratio"] == 0.5
    cluster_names = [c["name"] for c in data["clusters"]]
    assert "被排除人" not in cluster_names
    assert set(cluster_names) == {"王教授", "张三"}


async def test_homonym_collision_not_merged_and_emits_warning(
    client: AsyncClient,
) -> None:
    """Test 4: Two '张伟' with different student IDs or different emails are NOT merged and emit BATCH_NAME_COLLISION."""
    await _seed_admin()
    headers = await _login_headers(client)

    docx_1 = _build_proposal_docx(
        project_title="同名冲突课题1",
        applicant_name="刘教授",
        fill_date="2024年3月1日",
        members=[
            ("张伟", "2431101111", "硕士生", "天津工业大学", ""),
            ("李敏", "", "讲师", "天津工业大学", "limin_a@tiangong.edu.cn"),
        ],
    )
    docx_2 = _build_proposal_docx(
        project_title="同名冲突课题2",
        applicant_name="刘教授",
        fill_date="2025年3月1日",
        members=[
            ("张伟", "2431102222", "硕士生", "天津工业大学", ""),
            ("李敏", "", "讲师", "天津工业大学", "limin_b@tiangong.edu.cn"),
        ],
    )
    r1 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("col1.docx", docx_1, "application/octet-stream")},
        headers=headers,
    )
    r2 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("col2.docx", docx_2, "application/octet-stream")},
        headers=headers,
    )
    j1_id = r1.json()["job_id"]
    j2_id = r2.json()["job_id"]

    res = await client.post(
        "/api/v1/admin/imports/batch/resolve",
        json={"job_ids": [j1_id, j2_id]},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()

    # 刘教授 (1 cluster) + 张伟 (2 separate clusters) + 李敏 (2 separate clusters) = 5 clusters
    assert data["quality"]["people_total"] == 5
    assert "张伟" in data["quality"]["name_collisions"]
    assert "李敏" in data["quality"]["name_collisions"]

    zw_clusters = [c for c in data["clusters"] if c["name"] == "张伟"]
    assert len(zw_clusters) == 2
    assert {c["student_id"] for c in zw_clusters} == {"2431101111", "2431102222"}

    lm_clusters = [c for c in data["clusters"] if c["name"] == "李敏"]
    assert len(lm_clusters) == 2
    assert {c["email"] for c in lm_clusters} == {
        "limin_a@tiangong.edu.cn",
        "limin_b@tiangong.edu.cn",
    }

    for job_obj in data["jobs"]:
        for p in job_obj["preview"]["people"]:
            if p["name"] in ("张伟", "李敏"):
                assert "BATCH_NAME_COLLISION" in p["warnings"]


async def test_batch_provision_transaction_rollback_and_validation_guards(
    client: AsyncClient,
    monkeypatch,
) -> None:
    """Test 5: If Job 2 fails during batch/provision, Job 1's User and LabPerson rows are rolled back."""
    await _seed_admin()
    headers = await _login_headers(client)

    # Validate batch size guards (empty or > 5)
    empty_res = await client.post(
        "/api/v1/admin/imports/batch/resolve",
        json={"job_ids": []},
        headers=headers,
    )
    assert empty_res.status_code == 422
    assert empty_res.json()["error"]["code"] == "batch_size_invalid"

    too_many_res = await client.post(
        "/api/v1/admin/imports/batch/resolve",
        json={"job_ids": [str(uuid.uuid4()) for _ in range(6)]},
        headers=headers,
    )
    assert too_many_res.status_code == 422
    assert too_many_res.json()["error"]["code"] == "batch_size_invalid"

    docx_1 = _build_proposal_docx(
        project_title="回滚测试课题1",
        applicant_name="陈一",
        fill_date="2024年4月1日",
        members=[("孙二", "2431103333", "硕士生", "天津工业大学", "")],
    )
    docx_2 = _build_proposal_docx(
        project_title="回滚测试课题2",
        applicant_name="周三",
        fill_date="2025年4月1日",
        members=[("吴四", "2431104444", "硕士生", "天津工业大学", "")],
    )
    r1 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("rb1.docx", docx_1, "application/octet-stream")},
        headers=headers,
    )
    r2 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("rb2.docx", docx_2, "application/octet-stream")},
        headers=headers,
    )
    j1_id = r1.json()["job_id"]
    j2_id = r2.json()["job_id"]

    # Production default password guard test
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "prod")
    prod_fail = await client.post(
        "/api/v1/admin/imports/batch/provision",
        json={
            "job_ids": [j1_id, j2_id],
            "initial_password": settings.import_default_password,
        },
        headers=headers,
    )
    assert prod_fail.status_code == 422
    assert prod_fail.json()["error"]["code"] == "initial_password_required"
    monkeypatch.setattr(settings, "app_env", "test")

    # Trigger failure on Job 2 via invalid link_user_id override
    fail_resp = await client.post(
        "/api/v1/admin/imports/batch/provision",
        json={
            "job_ids": [j1_id, j2_id],
            "initial_password": INITIAL_PW,
            "jobs": [
                {"job_id": j1_id},
                {
                    "job_id": j2_id,
                    "people": [
                        {"key": "p1", "link_user_id": str(uuid.uuid4())},
                    ],
                },
            ],
        },
        headers=headers,
    )
    assert fail_resp.status_code == 422
    assert fail_resp.json()["error"]["code"] == "link_user_invalid"

    # Verify Job 1's created User (陈一, 孙二) and LabPerson rows were completely rolled back
    # and both jobs remain in status="parsed"
    maker = get_sessionmaker()
    async with maker() as session:
        users = list(
            await session.scalars(
                select(User).where(User.display_name.in_(("陈一", "孙二", "周三", "吴四")))
            )
        )
        assert users == []
        people = list(await session.scalars(select(LabPerson)))
        assert people == []
        j1_db = await session.get(ImportJob, uuid.UUID(j1_id))
        j2_db = await session.get(ImportJob, uuid.UUID(j2_id))
        assert j1_db is not None and j1_db.status == "parsed"
        assert j2_db is not None and j2_db.status == "parsed"


async def test_signature_precedence_and_individual_job_revert_in_batch(
    client: AsyncClient,
) -> None:
    """Test 6: Newer job's docx_import signature overrides older job's docx_import signature,
    manual upload signature is never overwritten, and reverting Job 2 keeps Job 1 intact."""
    await _seed_admin()
    headers = await _login_headers(client)

    sig_2024_zs = make_png_bytes(60, 28, fill_byte=0x22)
    sig_2024_ls = make_png_bytes(64, 28, fill_byte=0x33)
    sig_2025_zs = make_png_bytes(70, 30, fill_byte=0x55)
    sig_2025_ls = make_png_bytes(72, 30, fill_byte=0x66)
    manual_sig_ls = make_png_bytes(80, 32, fill_byte=0x99)

    sha_2024_zs = hashlib.sha256(sig_2024_zs).hexdigest()
    sha_2025_zs = hashlib.sha256(sig_2025_zs).hexdigest()
    sha_manual_ls = hashlib.sha256(manual_sig_ls).hexdigest()

    docx_2024 = _build_proposal_docx(
        project_title="签名测试2024",
        applicant_name="王教授",
        applicant_title="副教授",
        fill_date="2024年3月15日",
        members=[
            ("张三", "2431101262", "硕士生", "天津工业大学", ""),
            ("李四", "2431101263", "讲师", "天津工业大学", ""),
        ],
        foundation_text=(
            "前期研究基础\n"
            "1．[学术简历] 王教授，人工智能方向。\n"
            "2．[前期成果]\n"
            "（1）王教授（第一）：《2024基础论文》，计算机学报，2024年。\n"
            "3．[承担项目]\n"
            "（1）《跨年度研究项目》（AGE240005），王教授（主持），国家自然科学基金，2024年1月立项，在研。\n"
        ),
        member_signatures={0: sig_2024_zs, 1: sig_2024_ls},
    )
    docx_2025 = _build_proposal_docx(
        project_title="签名测试2025",
        applicant_name="王教授",
        applicant_title="教授",
        fill_date="2025年6月15日",
        members=[
            ("张三", "2431101262", "博士生", "天津工业大学", ""),
            ("李四", "2431101263", "副教授", "天津工业大学", ""),
        ],
        foundation_text=(
            "前期研究基础\n"
            "1．[学术简历] 王教授，人工智能与大模型安全方向。\n"
            "2．[前期成果]\n"
            "（1）王教授（第一）：《2025新成果论文》，软件学报，2025年。\n"
            "3．[承担项目]\n"
            "（1）《跨年度研究项目》（AGE240005），王教授（主持），国家自然科学基金，2024年1月立项，已结题。\n"
        ),
        member_signatures={0: sig_2025_zs, 1: sig_2025_ls},
    )

    r1 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("sig_2024.docx", docx_2024, "application/octet-stream")},
        headers=headers,
    )
    r2 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("sig_2025.docx", docx_2025, "application/octet-stream")},
        headers=headers,
    )
    assert r1.status_code == 200 and r2.status_code == 200
    j1_id = r1.json()["job_id"]
    j2_id = r2.json()["job_id"]

    # Pre-seed 李四 with a manual upload signature before batch/provision to verify upload is never overwritten
    maker = get_sessionmaker()
    async with maker() as session:
        u_ls = User(
            login_name="2431101263",
            display_name="李四",
            role="member",
            password_hash=hash_password(INITIAL_PW),
            is_active=True,
        )
        session.add(u_ls)
        await session.flush()
        from app.importer.fingerprint import person_fp

        lp_ls = LabPerson(
            user_id=u_ls.id,
            name="李四",
            title="讲师",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint=person_fp(
                tier="T1",
                login_name="2431101263",
                name="李四",
                affiliation="天津工业大学",
            ),
            as_of_time=datetime(2024, 1, 1, tzinfo=UTC).date(),
            source_import_id=uuid.UUID(j1_id),
            signature_data=manual_sig_ls,
            signature_mime="image/png",
            signature_sha256=sha_manual_ls,
            signature_updated_at=datetime.now(UTC),
            signature_source="upload",
        )
        session.add(lp_ls)
        await session.commit()

    # Batch provision Job 1 and Job 2
    prov_resp = await client.post(
        "/api/v1/admin/imports/batch/provision",
        json={
            "job_ids": [j1_id, j2_id],
            "initial_password": INITIAL_PW,
        },
        headers=headers,
    )
    assert prov_resp.status_code == 200, prov_resp.text
    prov_data = prov_resp.json()
    assert prov_data["merge_queue"] == [j2_id]

    # Run merge on merge_queue (Job 2)
    merge_resp = await client.post(
        f"/api/v1/admin/imports/{j2_id}/merge",
        json={},
        headers=headers,
    )
    assert merge_resp.status_code == 200, merge_resp.text

    # Check state after merge:
    # 1. 张三 has Job 2's newer docx_import signature (sha_2025_zs) and title="博士生"
    # 2. 李四 still has manual upload signature (sha_manual_ls, signature_source="upload")
    # 3. Project AGE240005 has status="completed" (from 2025)
    async with maker() as session:
        zs = await session.scalar(select(LabPerson).where(LabPerson.name == "张三"))
        ls = await session.scalar(select(LabPerson).where(LabPerson.name == "李四"))
        proj = await session.scalar(
            select(LabProject).where(LabProject.grant_no == "AGE240005")
        )
        achs = list(await session.scalars(select(LabAchievement)))
        assert zs is not None
        assert zs.signature_sha256 == sha_2025_zs
        assert zs.signature_source == "docx_import"
        assert zs.title == "博士生"
        assert ls is not None
        assert ls.signature_sha256 == sha_manual_ls
        assert ls.signature_source == "upload"
        assert proj is not None and proj.status == "completed"
        assert len(achs) == 2

    # Now revert Job 2 only via POST /api/v1/admin/imports/{j2_id}/revert
    rev_resp = await client.post(
        f"/api/v1/admin/imports/{j2_id}/revert",
        headers=headers,
    )
    assert rev_resp.status_code == 200, rev_resp.text

    # Verify Job 2's changes were reverted while Job 1 remains intact:
    # 1. 张三's signature restored to Job 1's sha_2024_zs, title restored to "硕士生"
    # 2. 李四's manual upload signature untouched
    # 3. Project AGE240005 status restored to "ongoing"
    # 4. Job 2's achievement deleted while Job 1's achievement remains
    async with maker() as session:
        zs_after = await session.scalar(
            select(LabPerson).where(LabPerson.name == "张三")
        )
        ls_after = await session.scalar(
            select(LabPerson).where(LabPerson.name == "李四")
        )
        proj_after = await session.scalar(
            select(LabProject).where(LabProject.grant_no == "AGE240005")
        )
        achs_after = list(await session.scalars(select(LabAchievement)))
        assert zs_after is not None
        assert zs_after.signature_sha256 == sha_2024_zs
        assert zs_after.signature_source == "docx_import"
        assert zs_after.title == "硕士生"
        assert ls_after is not None
        assert ls_after.signature_sha256 == sha_manual_ls
        assert ls_after.signature_source == "upload"
        assert proj_after is not None and proj_after.status == "ongoing"
        assert len(achs_after) == 1
        assert "2024基础论文" in achs_after[0].title


async def test_self_candidate_and_link_self_cluster_id_preserves_admin_role(
    client: AsyncClient,
) -> None:
    """Test 7: self_candidate identifies the admin's cluster and link_self_cluster_id links
    the admin's User.id to their LabPerson across the batch without altering role='admin' or password."""
    admin = await _seed_admin(display_name="万振凯", login_name="102001")
    headers = await _login_headers(client)

    docx_a = _build_proposal_docx(
        project_title="PI绑定课题A",
        applicant_name="万振凯",
        applicant_title="教授",
        fill_date="2024年3月1日",
        members=[("林雪莹", "2431101262", "硕士生", "天津工业大学", "")],
    )
    docx_b = _build_proposal_docx(
        project_title="PI绑定课题B",
        applicant_name="万振凯",
        applicant_title="教授",
        fill_date="2025年3月1日",
        members=[("林雪莹", "", "硕士生", "天津工业大学", "")],
    )

    ra = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("pi_a.docx", docx_a, "application/octet-stream")},
        headers=headers,
    )
    rb = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("pi_b.docx", docx_b, "application/octet-stream")},
        headers=headers,
    )
    ja_id = ra.json()["job_id"]
    jb_id = rb.json()["job_id"]

    res = await client.post(
        "/api/v1/admin/imports/batch/resolve",
        json={"job_ids": [ja_id, jb_id]},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    self_cand = res.json()["self_candidate"]
    assert self_cand is not None
    assert self_cand["name"] == "万振凯"
    assert len(self_cand["members"]) == 2
    cluster_id = self_cand["cluster_id"]

    # Provision with link_self_cluster_id
    prov_resp = await client.post(
        "/api/v1/admin/imports/batch/provision",
        json={
            "job_ids": [ja_id, jb_id],
            "initial_password": INITIAL_PW,
            "link_self_cluster_id": cluster_id,
        },
        headers=headers,
    )
    assert prov_resp.status_code == 200, prov_resp.text
    prov_data = prov_resp.json()

    # Only 1 new account created (林雪莹); 万振凯 is listed under existing accounts once
    assert "2431101262" in prov_data["roster_text"]
    assert "以下成员已有账号" in prov_data["roster_text"]
    assert prov_data["roster_text"].count("万振凯") == 1

    # Verify in DB: admin's LabPerson is linked to admin.id, and admin keeps role="admin" and original password
    maker = get_sessionmaker()
    async with maker() as session:
        admin_db = await session.get(User, admin.id)
        assert admin_db is not None
        assert admin_db.role == "admin"
        assert admin_db.must_change_password_by is None

        wan_lp = await session.scalar(
            select(LabPerson).where(LabPerson.name == "万振凯")
        )
        assert wan_lp is not None
        assert wan_lp.user_id == admin.id

    # Admin can still log in with original ADMIN_PASSWORD
    relogin = await _login_headers(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert relogin

    # Subsequent batch/resolve when admin already has a linked LabPerson returns self_candidate=None
    docx_c = _build_proposal_docx(
        project_title="PI已绑定后续课题C",
        applicant_name="万振凯",
        fill_date="2026年3月1日",
        members=[("林雪莹", "2431101262", "硕士生", "天津工业大学", "")],
    )
    rc = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("pi_c.docx", docx_c, "application/octet-stream")},
        headers=headers,
    )
    jc_id = rc.json()["job_id"]
    res_after = await client.post(
        "/api/v1/admin/imports/batch/resolve",
        json={"job_ids": [jc_id]},
        headers=headers,
    )
    assert res_after.status_code == 200
    assert res_after.json()["self_candidate"] is None
