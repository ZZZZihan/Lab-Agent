"""End-to-end tests for PR-31 Proposal Cold-Start Import, 3-Tier Provisioning, 3-in-1 Login, Lab Assets Hub, and RAG."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import docx
from app.auth import hash_password
from app.db import get_sessionmaker
from app.importer.pii import assert_no_pii
from app.models import Chunk, Document, DocumentACL, Group, User
from httpx import AsyncClient
from sqlalchemy import select, text

ADMIN_EMAIL = "admin@lab.local"
ADMIN_PASSWORD = "LabAgent-Test-Password-2026"
INITIAL_PW = "LabAgent@2026"

_ID18_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID18_CHECK_CHARS = "10X98765432"


def _make_synthetic_id18(prefix17: str) -> str:
    assert len(prefix17) == 17 and prefix17.isdigit()
    total = sum(int(ch) * w for ch, w in zip(prefix17, _ID18_WEIGHTS, strict=True))
    return prefix17 + _ID18_CHECK_CHARS[total % 11]


SYNTH_ID_APPLICANT = _make_synthetic_id18("11010119700101101")
SYNTH_ID_M1 = _make_synthetic_id18("12010119850505201")
SYNTH_ID_M2 = _make_synthetic_id18("12010119860606202")
SYNTH_ID_M3 = _make_synthetic_id18("12010119870707203")
SYNTH_ID_M4 = _make_synthetic_id18("12010119880808204")
SYNTH_ID_M5 = _make_synthetic_id18("12010119890909205")
SYNTH_ID_M6 = _make_synthetic_id18("12010119901010206")
SYNTH_MOBILE_1 = "138" + "00138000"
SYNTH_MOBILE_2 = "139" + "12345678"
SYNTH_LANDLINE = "022-88881234"
STUDENT_BIND_EMAIL = "linxueying" + "@" + "tiangong.edu.cn"


def build_synthetic_proposal_docx(*, csv_Formula_affiliation: bool = False) -> bytes:
    """Dynamically construct a desensitized grant proposal `.docx` matching the 6-table structure."""
    doc = docx.Document()

    # Table 0: Cover table
    t1 = doc.add_table(rows=5, cols=2)
    t1_rows = [
        ("项 目 名 称", "教育考试专属大模型构建与安全应用研究"),
        ("申 请 人", "万振凯"),
        ("所 在 单 位", "天津天狮学院"),
        ("通 讯 地 址", "天津市武清区"),
        ("填 表 日 期", "2026 年 6 月 1 日"),
    ]
    for idx, (k, v) in enumerate(t1_rows):
        t1.rows[idx].cells[0].text = k
        t1.rows[idx].cells[1].text = v

    # Table 1: Team table (Applicant KV rows + Member header + 9 Member rows)
    t2 = doc.add_table(rows=15, cols=10)
    # Row 0: Section banner
    t2.rows[0].cells[0].text = "申请人基本情况"
    # Row 1: Name / Gender / Birth / Title / Duty
    r1_vals = ["姓名", "万振凯", "性别", "男", "出生年月", "197001", "专业技术职务", "教授", "行政职务", "院长"]
    for c_idx, val in enumerate(r1_vals):
        t2.rows[1].cells[c_idx].text = val
    # Row 2: Degree / Specialty / Tutor / Affair
    r2_vals = [
        "最终学位",
        "博士",
        "研究专长",
        "人工智能",
        "担任导师",
        "硕士生导师",
        "所在单位",
        "天津天狮学院",
        "电子信箱",
        "",
    ]
    for c_idx, val in enumerate(r2_vals):
        t2.rows[2].cells[c_idx].text = val
    # Row 3: PII fields on applicant (ID card + mobile phone)
    r3_vals = [
        "证件类型",
        "身份证",
        "证件号码",
        SYNTH_ID_APPLICANT,
        "联系电话",
        SYNTH_MOBILE_1,
        "",
        "",
        "",
        "",
    ]
    for c_idx, val in enumerate(r3_vals):
        t2.rows[3].cells[c_idx].text = val

    # Row 4: Member header
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

    second_wan_aff = "=cmd|' /C calc'!A0" if csv_Formula_affiliation else "南开大学"
    members = [
        # T3: Has real email + ID card (ID card must be dropped, tier=T3, login_name=guojianmin)
        ("1", "郭建民", SYNTH_ID_M1, "教授", "院长", "无", "大数据与云计算", "学士", "天津天狮学院", "guojianmin@tiangong.edu.cn"),
        # T2: Polyphone surnames (单 -> shan, 曾 -> zeng) + regular T2 + duplicate name 万振凯
        ("2", "单立飞", SYNTH_ID_M2, "副教授", "系主任", "无", "计算机技术", "硕士", "天津天狮学院", ""),
        ("3", "曾令伟", SYNTH_ID_M3, "副教授", "无", "无", "网络安全", "博士", "天津天狮学院", ""),
        ("4", "杨硕", SYNTH_ID_M4, "讲师", "无", "无", "知识图谱", "硕士", "天津天狮学院", ""),
        ("5", "李佳静", SYNTH_ID_M5, "讲师", "无", "无", "自然语言处理", "硕士", "天津天狮学院", ""),
        ("6", "万振凯", SYNTH_ID_M6, "副教授", "无", "硕士生导师", "机器学习", "博士", second_wan_aff, ""),
        # T1: 3 graduate students with 10-digit student IDs in 证件号码 column
        ("7", "林雪莹", "2431101262", "无", "无", "无", "深度学习", "学士", "天津工业大学", ""),
        ("8", "王诗涵", "2431101226", "无", "无", "无", "多模态大模型", "学士", "天津工业大学", ""),
        ("9", "曹梦轩", "2431111321", "无", "无", "无", "教育测评", "学士", "天津工业大学", ""),
    ]
    for idx, m in enumerate(members, start=5):
        for c_idx, val in enumerate(m):
            t2.rows[idx].cells[c_idx].text = val

    # Table 2: Budget / Facilities table
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

    # Table 3: Research Foundation (Table 4 in standard proposal)
    t4 = doc.add_table(rows=1, cols=1)
    t4.rows[0].cells[0].text = (
        "前期研究基础\n"
        f"1．[学术简历] 万振凯，天津天狮学院教授（办公室电话：{SYNTH_LANDLINE}，手机：{SYNTH_MOBILE_2}），"
        "长期从事人工智能与智能评卷系统研究。\n"
        "2．[前期成果]\n"
        "（1）万振凯（第一）：《数智融合赋能天津制造业高质量发展的对策建议》，研究报告，天津市科协，2025年。"
        "成果为大模型安全治理提供支撑。\n"
        "（2）万振凯（第一）：《基于深度学习的图像识别动态成果展》，成果展，天津市教委，2024年。"
        "验证了多模态评测可视化能力。\n"
        "（3）单立飞（第一）：《教育考试智能题库构建方法研究》，探索科学，2024年。"
        "提出了试题结构化抽取方法。\n"
        "（4）郭建民（第一）：《大规模在线考试异常行为检测算法》，时代教育，2023年。"
        "提升了考务风险识别准确率。\n"
        "（5）杨硕（第一）：《学科知识图谱自动对齐系统V1.0》，软件著作权，登记号2024SR001234，2024年。"
        "支撑自动命制与知识点映射。\n"
        "3．[承担项目]\n"
        "（1）《基于人工智能的智能评卷系统关键技术研究》（AGE240005），万振凯（主持），全国教育科学规划课题，2024年8月立项，在研。\n"
        "（2）《高考综合改革下命题质量智能评估研究》（TJYGZ59），万振凯（主持），天津市教育科学规划重大课题，2024年2月立项，在研。\n"
        "（3）《新工科背景下人工智能课程群建设》（2023JWZD17），郭建民（主持），天津市教委重点教改项目，2023年9月立项，在研。\n"
        "（4）《智能自适应学习诊断平台研发》（2019G49），万振凯（主持），天津市科技计划项目，2019年4月立项，已通过验收。\n"
        "（5）《海量教育测评数据隐私保护研究》（2017139），单立飞（参与），教育部人文社科项目，2017年6月立项，已结题。\n"
        "（6）《大模型安全对齐基准测试集构建》（津科智2025-08），曾令伟（主持），市级科研专项，2025.01-2027.06，经费15万元。\n"
        "4．[与已承担项目的关系] 本课题是在全国教育科学规划课题（AGE240005）基础上向大模型安全对齐方向的深入拓展。"
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


async def _login_headers(client: AsyncClient, identifier: str, password: str = ADMIN_PASSWORD) -> dict[str, str]:
    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": identifier, "password": password},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_parse_proposal_docx_and_zero_pii_residue(client: AsyncClient) -> None:
    await _seed_admin()
    headers = await _login_headers(client, ADMIN_EMAIL)
    raw_docx = build_synthetic_proposal_docx()

    resp = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "parsed"
    assert data["as_of_time"] == "2026-06-01"
    assert data["stats"]["people_parsed"] == 10
    assert data["stats"]["achievements_parsed"] == 5
    assert data["stats"]["projects_parsed"] == 6
    assert data["stats"]["facilities_parsed"] == 1
    assert data["stats"]["pii_dropped"] >= 9

    preview = data["preview"]
    people_by_key = {p["key"]: p for p in preview["people"]}
    # Applicant 万振凯 -> T2 wanzhenkai
    assert people_by_key["p1"]["name"] == "万振凯"
    assert people_by_key["p1"]["tier"] == "T2"
    assert people_by_key["p1"]["login_name"] == "wanzhenkai"
    assert people_by_key["p1"]["email"] is None
    assert "[已脱敏]" in (people_by_key["p1"].get("bio") or "")

    # Member 郭建民 -> T3 guojianmin + real email
    assert people_by_key["p2"]["name"] == "郭建民"
    assert people_by_key["p2"]["tier"] == "T3"
    assert people_by_key["p2"]["login_name"] == "guojianmin"
    assert people_by_key["p2"]["email"] == "guojianmin@tiangong.edu.cn"

    # Polyphone surnames: 单立飞 -> shanlifei, 曾令伟 -> zenglingwei
    assert people_by_key["p3"]["login_name"] == "shanlifei"
    assert people_by_key["p4"]["login_name"] == "zenglingwei"

    # Same-name collision: second 万振凯 -> wanzhenkai2 with LOGIN_NAME_SUFFIXED warning
    assert people_by_key["p7"]["name"] == "万振凯"
    assert people_by_key["p7"]["login_name"] == "wanzhenkai2"
    assert "LOGIN_NAME_SUFFIXED" in people_by_key["p7"]["warnings"]

    # T1 graduate students: 10-digit student IDs preserved as login_name, email is None
    assert people_by_key["p8"]["tier"] == "T1"
    assert people_by_key["p8"]["login_name"] == "2431101262"
    assert people_by_key["p8"]["email"] is None

    # Verify inferred project status against as_of_time=2026-06-01
    proj_by_key = {j["key"]: j for j in preview["projects"]}
    assert proj_by_key["j1"]["grant_no"] == "AGE240005"
    assert proj_by_key["j1"]["status"] == "ongoing"
    assert proj_by_key["j1"]["status_source"] == "explicit"
    assert proj_by_key["j4"]["grant_no"] == "2019G49"
    assert proj_by_key["j4"]["status"] == "completed"
    assert proj_by_key["j6"]["status"] == "ongoing"
    assert proj_by_key["j6"]["status_source"] == "inferred"

    # Assert zero PII in HTTP response and DB preview JSONB
    raw_json_str = resp.text
    for secret_pii in (
        SYNTH_ID_APPLICANT,
        SYNTH_ID_M1,
        SYNTH_ID_M2,
        SYNTH_MOBILE_1,
        SYNTH_MOBILE_2,
        SYNTH_LANDLINE,
    ):
        assert secret_pii not in raw_json_str
    assert_no_pii(preview)

    # Verify _dedup_people preserves two people with the same (name, affiliation) when their student IDs differ
    from app.importer.formatters import format_project_application
    from app.importer.parser import _dedup_people

    distinct_pair = _dedup_people(
        [
            {"name": "张伟", "affiliation": "天津师范大学", "cert": "2431101262"},
            {"name": "张伟", "affiliation": "天津师范大学", "cert": "2431101226"},
        ]
    )
    assert len(distinct_pair) == 2

    # Verify unknown project status does not claim "进行中"
    unk_text = format_project_application(
        {"title": "未注明状态项目", "pi_name": "万振凯", "lab_role": "host", "status": "unknown"}
    )
    assert "进行中" not in unk_text
    assert "状态未注明" in unk_text


async def test_malicious_or_invalid_docx_rejected(client: AsyncClient) -> None:
    await _seed_admin()
    headers = await _login_headers(client, ADMIN_EMAIL)

    # 1. Wrong extension (.doc)
    r_doc = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("legacy.doc", b"PK\x03\x04fake", "application/msword")},
        headers=headers,
    )
    assert r_doc.status_code == 415
    assert r_doc.json()["error"]["code"] == "import_unsupported_format"

    # 2. Fake magic bytes with .docx extension
    r_magic = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("fake.docx", b"NOT_A_ZIP_FILE", "application/octet-stream")},
        headers=headers,
    )
    assert r_magic.status_code == 415
    assert r_magic.json()["error"]["code"] == "import_unsupported_format"

    # 3. Macro-enabled .docx containing word/vbaProject.bin
    macro_buf = io.BytesIO()
    with zipfile.ZipFile(macro_buf, "w") as zf:
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("word/vbaProject.bin", b"malicious_macro")
    r_macro = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("macro.docx", macro_buf.getvalue(), "application/octet-stream")},
        headers=headers,
    )
    assert r_macro.status_code == 415
    assert r_macro.json()["error"]["code"] == "import_unsupported_format"

    # 4. XML entity (XXE) in word/document.xml
    xxe_buf = io.BytesIO()
    with zipfile.ZipFile(xxe_buf, "w") as zf:
        zf.writestr("word/document.xml", '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><w:document/>')
    r_xxe = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("xxe.docx", xxe_buf.getvalue(), "application/octet-stream")},
        headers=headers,
    )
    assert r_xxe.status_code == 415
    assert r_xxe.json()["error"]["code"] == "import_unsupported_format"

    # 5. Valid binary OLE2 .doc proposal upload succeeds
    from app.importer.reader import (
        build_ole2_doc_bytes,
        extract_table_grid,
        iter_document_tables,
        validate_and_load_docx,
    )

    synth_docx_bytes = build_synthetic_proposal_docx()
    docx_obj = validate_and_load_docx("proposal.docx", synth_docx_bytes)
    docx_grids = [extract_table_grid(t) for t in iter_document_tables(docx_obj)]
    ole2_proposal_bytes = build_ole2_doc_bytes(tables=docx_grids)
    r_valid_doc = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal-legacy.doc", ole2_proposal_bytes, "application/msword")},
        headers=headers,
    )
    assert r_valid_doc.status_code == 200, r_valid_doc.text
    doc_payload = r_valid_doc.json()
    assert doc_payload["stats"]["people_parsed"] == 10
    assert doc_payload["stats"]["projects_parsed"] == 6
    assert doc_payload["stats"]["achievements_parsed"] == 5
    assert doc_payload["stats"]["facilities_parsed"] == 1
    assert doc_payload["stats"]["pii_dropped"] >= 9


async def test_provision_hardcodes_member_role_and_preserves_existing_admin(
    client: AsyncClient,
) -> None:
    await _seed_admin()
    # Pre-create an existing admin user with email guojianmin@tiangong.edu.cn (matches T3 郭建民)
    existing_admin_pw = "AdminKeepPw-9988!"
    maker = get_sessionmaker()
    async with maker() as session:
        existing_guo = User(
            email="guojianmin@tiangong.edu.cn",
            display_name="郭建民",
            role="admin",
            password_hash=hash_password(existing_admin_pw),
            is_active=True,
        )
        session.add(existing_guo)
        await session.commit()

    headers = await _login_headers(client, ADMIN_EMAIL)
    raw_docx = build_synthetic_proposal_docx(csv_Formula_affiliation=True)

    parse_resp = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    assert parse_resp.status_code == 200
    job_id = parse_resp.json()["job_id"]

    # Attempting to pass a "role" field in ProvisionRequest must be rejected by extra="forbid" (422)
    bad_role_resp = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        json={"role": "admin"},
        headers=headers,
    )
    assert bad_role_resp.status_code == 422

    # Provision with include_password=False first on a separate parse, or test normal provision
    prov_resp = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        json={"initial_password": INITIAL_PW, "include_password": True},
        headers=headers,
    )
    assert prov_resp.status_code == 200, prov_resp.text
    res = prov_resp.json()
    assert res["status"] == "provisioned"
    assert res["stats"]["users_created"] == 9
    assert res["stats"]["users_linked"] == 1
    assert res["stats"]["people_inserted"] == 10
    assert res["stats"]["projects_inserted"] == 6
    assert res["stats"]["achievements_inserted"] == 5
    assert res["stats"]["facilities_inserted"] == 1

    # Check CSV UTF-8 BOM and CSV formula escaping (`'=cmd|...`)
    csv_text: str = res["roster_csv"]
    assert csv_text.startswith("﻿")
    assert "'=cmd|" in csv_text
    assert "wanzhenkai2" in res["roster_text"]

    # Verify in DB:
    # 1. Existing admin 郭建民 kept role="admin", original password, and must_change_password_by is None
    # 2. All 9 newly created users have role="member" and must_change_password_by is not None
    # 3. All T1/T2 users have email IS NULL (zero fake emails)
    async with maker() as session:
        all_users = list(await session.scalars(select(User)))
        guo = next(u for u in all_users if u.email == "guojianmin@tiangong.edu.cn")
        assert guo.role == "admin"
        assert guo.must_change_password_by is None

        created_users = [
            u for u in all_users if u.email not in (ADMIN_EMAIL, "guojianmin@tiangong.edu.cn")
        ]
        assert len(created_users) == 9
        for u in created_users:
            assert u.role == "member"
            assert u.email is None
            assert u.login_name is not None
            assert u.must_change_password_by is not None

    # Existing admin 郭建民 can still log in with his original password
    guo_headers = await _login_headers(client, "guojianmin@tiangong.edu.cn", existing_admin_pw)
    assert guo_headers

    # Re-provisioning the same job_id returns 409 import_already_provisioned
    dup_resp = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        json={},
        headers=headers,
    )
    assert dup_resp.status_code == 409
    assert dup_resp.json()["error"]["code"] == "import_already_provisioned"

    # Re-importing the exact same .docx in a second job skips existing fingerprints idempotently
    parse2 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    assert parse2.status_code == 200
    job2_id = parse2.json()["job_id"]
    prov2 = await client.post(
        f"/api/v1/admin/imports/{job2_id}/provision",
        json={"include_password": False},
        headers=headers,
    )
    assert prov2.status_code == 200, prov2.text
    res2 = prov2.json()
    assert "初始口令：另行私发" in res2["roster_text"]
    assert res2["stats"]["users_created"] == 0
    assert res2["stats"]["users_linked"] == 10
    assert res2["stats"]["people_skipped"] == 10
    assert res2["stats"]["projects_skipped"] == 6
    assert res2["stats"]["achievements_skipped"] == 5
    assert res2["stats"]["facilities_skipped"] == 1

    async with maker() as session:
        all_users_after = list(await session.scalars(select(User)))
        assert len(all_users_after) == 11


async def test_three_in_one_login_password_change_and_expiry(
    client: AsyncClient,
) -> None:
    await _seed_admin()
    headers = await _login_headers(client, ADMIN_EMAIL)
    raw_docx = build_synthetic_proposal_docx()

    parse_resp = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    job_id = parse_resp.json()["job_id"]
    prov_resp = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        json={"initial_password": INITIAL_PW},
        headers=headers,
    )
    assert prov_resp.status_code == 200

    # 1. T1 student ID login (2431101262 -> 林雪莹)
    r_t1 = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "2431101262", "password": INITIAL_PW},
    )
    assert r_t1.status_code == 200
    assert r_t1.json()["user"]["display_name"] == "林雪莹"
    assert r_t1.json()["user"]["must_change_password"] is True

    # 2. T2 full pinyin login (shanlifei -> 单立飞)
    r_t2_py = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "shanlifei", "password": INITIAL_PW},
    )
    assert r_t2_py.status_code == 200
    assert r_t2_py.json()["user"]["display_name"] == "单立飞"

    # 3. T2 unique Chinese name login (单立飞)
    r_t2_cn = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "单立飞", "password": INITIAL_PW},
    )
    assert r_t2_cn.status_code == 200
    assert r_t2_cn.json()["user"]["login_name"] == "shanlifei"

    # 4. Ambiguous Chinese name login (万振凯 has 2 accounts: wanzhenkai and wanzhenkai2) -> 401 ambiguous_name
    r_ambig = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "万振凯", "password": INITIAL_PW},
    )
    assert r_ambig.status_code == 401
    assert r_ambig.json()["error"]["code"] == "ambiguous_name"

    # Disambiguated login with wanzhenkai and wanzhenkai2 both succeed
    r_wan1 = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "wanzhenkai", "password": INITIAL_PW},
    )
    assert r_wan1.status_code == 200
    r_wan2 = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "wanzhenkai2", "password": INITIAL_PW},
    )
    assert r_wan2.status_code == 200

    # 5. T3 login via BOTH pinyin (guojianmin) and email (guojianmin@tiangong.edu.cn)
    r_t3_py = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "guojianmin", "password": INITIAL_PW},
    )
    assert r_t3_py.status_code == 200
    r_t3_em = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "guojianmin@tiangong.edu.cn", "password": INITIAL_PW},
    )
    assert r_t3_em.status_code == 200

    # 6. Self password change + optional email binding for T1 student (2431101262)
    r_attacker = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "2431101262", "password": INITIAL_PW},
    )
    assert r_attacker.status_code == 200
    attacker_headers = {"Authorization": f"Bearer {r_attacker.json()['access_token']}"}

    t1_token = r_t1.json()["access_token"]
    t1_headers = {"Authorization": f"Bearer {t1_token}"}

    # While must_change_password_by is pending: /auth/me is allowed, but /lab/overview is blocked with 403
    me_before = await client.get("/api/v1/auth/me", headers=t1_headers)
    assert me_before.status_code == 200
    assert me_before.json()["must_change_password"] is True

    blocked_resp = await client.get("/api/v1/lab/overview", headers=t1_headers)
    assert blocked_resp.status_code == 403
    assert blocked_resp.json()["error"]["code"] == "password_change_required"

    new_student_pw = "SafePass-2026#Student"
    chg_resp = await client.post(
        "/api/v1/auth/change-password",
        json={
            "old_password": INITIAL_PW,
            "new_password": new_student_pw,
            "email": STUDENT_BIND_EMAIL,
        },
        headers=t1_headers,
    )
    assert chg_resp.status_code == 200, chg_resp.text
    updated_user = chg_resp.json()["user"]
    assert updated_user["must_change_password"] is False
    assert updated_user["email"] == STUDENT_BIND_EMAIL
    assert chg_resp.json().get("access_token")

    # After changing password, owner session (and new token) is allowed, while other pre-existing session is invalidated (401)
    allowed_after = await client.get("/api/v1/lab/overview", headers=t1_headers)
    assert allowed_after.status_code == 200

    attacker_after = await client.get("/api/v1/lab/overview", headers=attacker_headers)
    assert attacker_after.status_code == 401
    attacker_me = await client.get("/api/v1/auth/me", headers=attacker_headers)
    assert attacker_me.status_code == 401

    # Student can now log in with newly bound email and new password
    r_t1_email = await client.post(
        "/api/v1/auth/login",
        json={"identifier": STUDENT_BIND_EMAIL, "password": new_student_pw},
    )
    assert r_t1_email.status_code == 200

    # 7. Expired initial password rejects login with 403 initial_password_expired
    maker = get_sessionmaker()
    async with maker() as session:
        shan = await session.scalar(select(User).where(User.login_name == "shanlifei"))
        assert shan is not None
        shan_id = shan.id
        shan.must_change_password_by = datetime.now(UTC) - timedelta(days=1)
        await session.commit()

    r_exp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "shanlifei", "password": INITIAL_PW},
    )
    assert r_exp.status_code == 403
    assert r_exp.json()["error"]["code"] == "initial_password_expired"

    # Admin resets shanlifei's password -> clears expiry and allows login
    reset_pw = "ShanReset-2026#Ok"
    r_reset = await client.post(
        f"/api/v1/admin/users/{shan_id}/password",
        json={"password": reset_pw},
        headers=headers,
    )
    assert r_reset.status_code == 200
    r_shan_after = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "shanlifei", "password": reset_pw},
    )
    assert r_shan_after.status_code == 200

    # CLI reset-password also clears must_change_password_by and supports login_name lookup
    from app.admin_cli import _run, build_parser

    async with maker() as session:
        shan = await session.scalar(select(User).where(User.login_name == "shanlifei"))
        assert shan is not None
        shan.must_change_password_by = datetime.now(UTC) - timedelta(days=1)
        await session.commit()

    cli_args = build_parser().parse_args(
        ["reset-password", "--email", "shanlifei", "--password-stdin"]
    )
    cli_pw = "ShanCliReset-2026#Ok"
    rc = await _run(cli_args, cli_pw)
    assert rc == 0
    r_shan_cli = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "shanlifei", "password": cli_pw},
    )
    assert r_shan_cli.status_code == 200
    assert r_shan_cli.json()["user"]["must_change_password"] is False

    # Two T3 members with identical (name, affiliation) and distinct emails get distinct fingerprints
    from app.importer.people import assign_people_identities

    t3_twins: list[dict[str, object]] = [
        {"name": "张同名", "affiliation": "计算机学院", "email": "ztm1@example.edu.cn"},
        {"name": "张同名", "affiliation": "计算机学院", "email": "ztm2@example.edu.cn"},
    ]
    assign_people_identities(t3_twins)
    assert t3_twins[0]["fingerprint"] != t3_twins[1]["fingerprint"]
    assert t3_twins[0]["login_name"] == "zhangtongming"
    assert t3_twins[1]["login_name"] == "zhangtongming2"


async def test_lab_assets_api_formatters_and_rag_chat_integration(
    client: AsyncClient,
) -> None:
    await _seed_admin()
    headers = await _login_headers(client, ADMIN_EMAIL)
    raw_docx = build_synthetic_proposal_docx()

    parse_resp = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    job_id = parse_resp.json()["job_id"]
    prov_resp = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        json={"initial_password": INITIAL_PW},
        headers=headers,
    )
    assert prov_resp.status_code == 200

    # Check /api/v1/lab/overview
    ov = await client.get("/api/v1/lab/overview", headers=headers)
    assert ov.status_code == 200
    ov_data = ov.json()
    assert ov_data["counts"]["people"] == 10
    assert ov_data["counts"]["projects"] == 6
    assert ov_data["counts"]["projects_ongoing"] == 4
    assert ov_data["counts"]["projects_completed"] == 2
    assert ov_data["counts"]["achievements"] == 5
    assert ov_data["counts"]["facilities"] == 1
    assert ov_data["as_of_time"] == "2026-06-01"

    # Check /api/v1/lab/people, /projects, /achievements, /facilities and application_text
    ppl = (await client.get("/api/v1/lab/people", headers=headers)).json()["people"]
    assert any("万振凯，博士，天津天狮学院、教授、院长、硕士生导师" in p["application_text"] for p in ppl)

    projs = (
        await client.get("/api/v1/lab/projects?status=ongoing", headers=headers)
    ).json()["projects"]
    assert len(projs) == 4
    assert any(
        "《基于人工智能的智能评卷系统关键技术研究》（AGE240005）" in j["application_text"]
        for j in projs
    )

    all_projs = (await client.get("/api/v1/lab/projects", headers=headers)).json()[
        "projects"
    ]
    participant_proj = next(j for j in all_projs if j["pi_name"] == "单立飞")
    assert participant_proj["lab_role"] == "participant"
    assert "单立飞参与：" in participant_proj["application_text"]
    assert "单立飞主持" not in participant_proj["application_text"]

    achs = (
        await client.get("/api/v1/lab/achievements?kind=report", headers=headers)
    ).json()["achievements"]
    assert len(achs) == 1
    assert "研究报告/资政报告" in achs[0]["application_text"]

    all_achs = (await client.get("/api/v1/lab/achievements", headers=headers)).json()[
        "achievements"
    ]
    venues = {a["venue"] for a in all_achs if a["venue"]}
    assert "探索科学" in venues
    assert "时代教育" in venues
    assert "2024年" not in venues

    # Verify _rerank_failed=True from search_knowledge_base is preserved on injected profile hits
    from unittest.mock import AsyncMock, patch

    from app.main import app as fastapi_app
    from app.tools import query_lab_assets

    maker = get_sessionmaker()
    async with maker() as session:
        admin_user = await session.scalar(select(User).where(User.email == ADMIN_EMAIL))
        assert admin_user is not None
        fake_failed_hit = {
            "chunk_id": "00000000-0000-0000-0000-000000000001",
            "document_id": "00000000-0000-0000-0000-000000000002",
            "doc_key": "other-doc",
            "chunk_index": 0,
            "title": "Other",
            "collection": "general",
            "heading": "H",
            "content": "C",
            "tokens": [],
            "cosine": 0.5,
            "bm25": 0.1,
            "rrf": 0.02,
            "_coverage": 0.5,
            "_elapsed_ms": 1.0,
            "_mode": "hybrid_rrf_rerank",
            "_rerank_failed": True,
        }
        with patch(
            "app.tools.search_knowledge_base",
            new=AsyncMock(return_value=[fake_failed_hit]),
        ):
            res_rf = await query_lab_assets(
                session,
                settings=fastapi_app.state.settings,
                embedder=fastapi_app.state.embedder,
                user=admin_user,
                query="课题组有哪些在研项目？",
            )
            rf_hits = res_rf["hits"]
            assert isinstance(rf_hits, list) and len(rf_hits) > 0
            assert rf_hits[0]["_rerank_failed"] is True

    facs = (await client.get("/api/v1/lab/facilities", headers=headers)).json()[
        "facilities"
    ]
    assert len(facs) == 1
    assert "8.00 万元" in facs[0]["application_text"]

    # Check RAG & Agent Q&A over /api/v1/chat/stream
    chat_resp = await client.post(
        "/api/v1/chat/stream",
        json={"message": "课题组有哪些在研项目？"},
        headers=headers,
    )
    assert chat_resp.status_code == 200
    events = [
        json.loads(block.split("data:", 1)[1].strip())
        for block in chat_resp.text.split("\n\n")
        if "data:" in block
    ]
    tool_events = [e for e in events if e.get("type") == "tool"]
    assert any(e.get("name") == "query_lab_assets" for e in tool_events)

    done_event = next(e for e in events if e.get("type") == "done")
    assert done_event["refused"] is False

    citations_event = next(e for e in events if e.get("type") == "citations")
    excerpts = " ".join(str(it.get("excerpt") or "") for it in citations_event["items"])
    assert "AGE240005" in excerpts or "TJYGZ59" in excerpts

    # Verify ACL enforcement on lab-profile-* document in query_lab_assets:
    # When admin changes the profile document visibility to "private", a regular member
    # must not receive its chunks via fallback injection.
    rag_doc_id = prov_resp.json()["rag_document_id"]
    acl_resp = await client.put(
        f"/api/v1/documents/{rag_doc_id}/acl",
        json={"visibility": "private"},
        headers=headers,
    )
    assert acl_resp.status_code == 200

    member_login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "2431101262", "password": INITIAL_PW},
    )
    assert member_login.status_code == 200
    member_headers = {"Authorization": f"Bearer {member_login.json()['access_token']}"}
    chg_member = await client.post(
        "/api/v1/auth/change-password",
        json={
            "old_password": INITIAL_PW,
            "new_password": "MemberSafe-2026#Pw",
        },
        headers=member_headers,
    )
    assert chg_member.status_code == 200
    member_chat = await client.post(
        "/api/v1/chat/stream",
        json={"message": "课题组有哪些在研项目？"},
        headers=member_headers,
    )
    assert member_chat.status_code == 200
    member_events = [
        json.loads(block.split("data:", 1)[1].strip())
        for block in member_chat.text.split("\n\n")
        if "data:" in block
    ]
    member_done = next(e for e in member_events if e.get("type") == "done")
    assert member_done["refused"] is True
    member_citations = next(
        (e for e in member_events if e.get("type") == "citations"),
        {"items": []},
    )
    assert member_citations["items"] == []

    # Verify zero PII across all database tables
    maker = get_sessionmaker()
    async with maker() as session:
        for tbl in ("import_jobs", "lab_people", "lab_projects", "lab_achievements", "lab_facilities", "chunks"):
            rows = await session.execute(text(f"SELECT row_to_json(t)::text FROM {tbl} t"))
            for (row_str,) in rows:
                assert SYNTH_ID_APPLICANT not in row_str
                assert SYNTH_ID_M1 not in row_str
                assert SYNTH_MOBILE_1 not in row_str
                assert SYNTH_MOBILE_2 not in row_str
                assert SYNTH_LANDLINE not in row_str


async def test_login_conflict_resolved_by_replacement_login_name(
    client: AsyncClient,
) -> None:
    await _seed_admin()
    # Pre-seed a user occupying T1 login_name "2431101262" with a different display_name
    maker = get_sessionmaker()
    async with maker() as session:
        session.add(
            User(
                email=None,
                login_name="2431101262",
                display_name="张占号",
                password_hash=hash_password(INITIAL_PW),
                role="member",
                is_active=True,
            )
        )
        await session.commit()

    headers = await _login_headers(client, ADMIN_EMAIL)
    raw_docx = build_synthetic_proposal_docx()

    parse_resp = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    assert parse_resp.status_code == 200
    job_data = parse_resp.json()
    job_id = job_data["job_id"]

    lin_person = next(
        p for p in job_data["preview"]["people"] if p["name"] == "林雪莹"
    )
    shan_person = next(
        p for p in job_data["preview"]["people"] if p["name"] == "单立飞"
    )
    assert lin_person["match"]["status"] == "conflict"
    assert "LOGIN_CONFLICT" in lin_person["warnings"]

    # Confirming without resolving the conflict fails with 422 login_conflict_unresolved
    fail_resp = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        json={"initial_password": INITIAL_PW},
        headers=headers,
    )
    assert fail_resp.status_code == 422
    assert fail_resp.json()["error"]["code"] == "login_conflict_unresolved"

    # Confirming with an unused replacement login_name clears the conflict and succeeds
    ok_resp = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        json={
            "initial_password": INITIAL_PW,
            "people": [
                {"key": lin_person["key"], "login_name": "linxueying2431"},
                {"key": shan_person["key"], "exclude": True},
            ],
        },
        headers=headers,
    )
    assert ok_resp.status_code == 200, ok_resp.text
    assert ok_resp.json()["status"] == "provisioned"

    # Downloaded roster reflects actual provisioning decisions (overridden login_name & excluded person omitted)
    roster_dl = await client.get(
        f"/api/v1/admin/imports/{job_id}/roster",
        headers=headers,
    )
    assert roster_dl.status_code == 200
    dl_text = roster_dl.json()["roster_text"]
    dl_csv = roster_dl.json()["roster_csv"]
    assert "linxueying2431" in dl_text
    assert "linxueying2431" in dl_csv
    assert "2431101262" not in dl_text
    assert "2431101262" not in dl_csv
    assert "单立飞" not in dl_text
    assert "单立飞" not in dl_csv
    assert "shanlifei" not in dl_text
    assert "shanlifei" not in dl_csv

    # 林雪莹 can now log in with the replacement login_name
    r_login = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "linxueying2431", "password": INITIAL_PW},
    )
    assert r_login.status_code == 200
    assert r_login.json()["user"]["display_name"] == "林雪莹"


async def test_oidc_mode_provisioning_and_login_name_claim_matching(
    client: AsyncClient,
) -> None:
    import uuid
    from unittest.mock import patch

    from app.auth import authenticate_oidc
    from app.main import app as fastapi_app

    await _seed_admin()
    headers = await _login_headers(client, ADMIN_EMAIL)
    raw_docx = build_synthetic_proposal_docx()

    parse_resp = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    assert parse_resp.status_code == 200
    job_id = parse_resp.json()["job_id"]

    settings = fastapi_app.state.settings
    orig_issuer = settings.oidc_issuer
    orig_audience = settings.oidc_audience
    orig_jwks = settings.oidc_jwks_json
    try:
        # Enable OIDC mode only after admin token is obtained, or provision directly via provision_import_job
        from app.importer.provision import ProvisionRequest, provision_import_job

        settings.oidc_issuer = "https://sso.example.edu.cn"
        settings.oidc_audience = "lab-agent"
        settings.oidc_jwks_json = '{"keys": [{"kid": "k1"}]}'

        maker = get_sessionmaker()
        async with maker() as session:
            admin_user = await session.scalar(
                select(User).where(User.email == ADMIN_EMAIL)
            )
            assert admin_user is not None
            res = await provision_import_job(
                session,
                job_id=uuid.UUID(job_id),
                actor_id=admin_user.id,
                body=ProvisionRequest(),
                settings=settings,
                embedder=fastapi_app.state.embedder,
            )
            await session.commit()

        assert res["initial_password"] is None
        assert res["deadline"] is None
        assert "统一身份认证 (OIDC SSO)" in str(res["roster_text"])

        async with maker() as session:
            lin = await session.scalar(
                select(User).where(User.login_name == "2431101262")
            )
            assert lin is not None
            assert lin.must_change_password_by is None

            # Verify _authenticate_oidc matches T1/T2 user by login_name claim when email is absent
            with (
                patch("app.auth.jwt.get_unverified_header", return_value={"kid": "k1"}),
                patch("app.auth.jwt.PyJWK.from_dict") as mock_jwk,
                patch(
                    "app.auth.jwt.decode",
                    return_value={
                        "sub": "oidc-sub-999",
                        "student_id": "2431101262",
                    },
                ),
            ):
                mock_jwk.return_value.key = "dummy-key"
                actor = await authenticate_oidc(session, "fake.jwt.token", settings)
                assert actor.id == lin.id
                assert actor.display_name == "林雪莹"
    finally:
        settings.oidc_issuer = orig_issuer
        settings.oidc_audience = orig_audience
        settings.oidc_jwks_json = orig_jwks


async def test_import_job_mgmt_roster_lab_crud_autoreindex_batch_and_export(
    client: AsyncClient,
) -> None:
    await _seed_admin()
    headers = await _login_headers(client, ADMIN_EMAIL)
    raw_docx = build_synthetic_proposal_docx()

    # 1. Upload via alias POST /api/v1/admin/imports & verify deduplication on unprovisioned parsed job
    r_up1 = await client.post(
        "/api/v1/admin/imports",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    assert r_up1.status_code == 200, r_up1.text
    temp_job_id = r_up1.json()["job_id"]
    assert r_up1.json()["deduplicated"] is False

    r_up_dup = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    assert r_up_dup.status_code == 200
    assert r_up_dup.json()["job_id"] == temp_job_id
    assert r_up_dup.json()["deduplicated"] is True

    # 2. List imports, get import detail & preview alias, patch preview, reparse, and delete parsed job
    r_list = await client.get("/api/v1/admin/imports", headers=headers)
    assert r_list.status_code == 200
    assert any(j["job_id"] == temp_job_id for j in r_list.json()["imports"])
    assert "preview" not in r_list.json()["imports"][0]

    r_detail = await client.get(f"/api/v1/admin/imports/{temp_job_id}", headers=headers)
    assert r_detail.status_code == 200
    assert "preview" in r_detail.json()

    r_prev_alias = await client.get(
        f"/api/v1/admin/imports/{temp_job_id}/preview", headers=headers
    )
    assert r_prev_alias.status_code == 200
    assert r_prev_alias.json()["job_id"] == temp_job_id

    # Patch preview (update as_of_time to 2028-01-01 and corrupt a project title)
    corrupted_projs = [dict(j) for j in r_prev_alias.json()["preview"]["projects"]]
    orig_first_proj_title = corrupted_projs[0]["title"]
    corrupted_projs[0]["title"] = "被篡改的错误项目标题"
    r_patch_prev = await client.patch(
        f"/api/v1/admin/imports/{temp_job_id}/preview",
        json={
            "as_of_time": "2028-01-01",
            "relationship_note": f"拓展说明（联系电话：{SYNTH_MOBILE_1}）",
            "projects": corrupted_projs,
        },
        headers=headers,
    )
    assert r_patch_prev.status_code == 200, r_patch_prev.text
    patched_prev = r_patch_prev.json()["preview"]
    assert r_patch_prev.json()["as_of_time"] == "2028-01-01"
    assert SYNTH_MOBILE_1 not in json.dumps(patched_prev, ensure_ascii=False)
    assert "[已脱敏]" in patched_prev["relationship_note"]
    assert patched_prev["projects"][0]["title"] == "被篡改的错误项目标题"
    j6_after_patch = next(j for j in patched_prev["projects"] if j["key"] == "j6")
    assert j6_after_patch["status"] == "completed"

    # Reparse with mode="llm" when llm_provider == "fake" -> 422 llm_reparse_unavailable
    r_reparse_llm = await client.post(
        f"/api/v1/admin/imports/{temp_job_id}/reparse",
        json={"mode": "llm"},
        headers=headers,
    )
    assert r_reparse_llm.status_code == 422
    assert r_reparse_llm.json()["error"]["code"] == "llm_reparse_unavailable"

    # Reparse with mode="fsm" -> restores corrupted title from source_snapshot
    r_reparse = await client.post(
        f"/api/v1/admin/imports/{temp_job_id}/reparse",
        json={"mode": "fsm"},
        headers=headers,
    )
    assert r_reparse.status_code == 200, r_reparse.text
    assert r_reparse.json()["parser_version"] == "v2.1-fsm"
    assert r_reparse.json()["preview"]["extraction_mode"] == "rule+regex_fsm"
    assert (
        r_reparse.json()["preview"]["projects"][0]["title"] == orig_first_proj_title
    )

    # Delete parsed job -> 200 deleted, then 404 on subsequent GET
    r_del_parsed = await client.delete(
        f"/api/v1/admin/imports/{temp_job_id}", headers=headers
    )
    assert r_del_parsed.status_code == 200
    assert r_del_parsed.json()["status"] == "deleted"

    r_after_del = await client.get(
        f"/api/v1/admin/imports/{temp_job_id}", headers=headers
    )
    assert r_after_del.status_code == 404

    # 3. Upload fresh job, provision it, verify DELETE returns 409 & roster endpoint has zero plaintext password
    r_up2 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    assert r_up2.status_code == 200
    job_id = r_up2.json()["job_id"]

    r_roster_before = await client.get(
        f"/api/v1/admin/imports/{job_id}/roster", headers=headers
    )
    assert r_roster_before.status_code == 409
    assert r_roster_before.json()["error"]["code"] == "import_not_provisioned"

    r_prov = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        json={"initial_password": INITIAL_PW, "include_password": True},
        headers=headers,
    )
    assert r_prov.status_code == 200, r_prov.text

    # Deleting a provisioned job must fail with 409 import_already_provisioned
    r_del_prov = await client.delete(
        f"/api/v1/admin/imports/{job_id}", headers=headers
    )
    assert r_del_prov.status_code == 409
    assert r_del_prov.json()["error"]["code"] == "import_already_provisioned"

    # Roster endpoint returns login names but NEVER plaintext password
    r_roster_txt = await client.get(
        f"/api/v1/admin/imports/{job_id}/roster?format=text", headers=headers
    )
    assert r_roster_txt.status_code == 200
    roster_payload = r_roster_txt.json()
    assert "wanzhenkai" in roster_payload["roster_text"]
    assert "2431101262" in roster_payload["roster_text"]
    assert "初始口令：（见开号时的通知）" in roster_payload["roster_text"]
    assert INITIAL_PW not in roster_payload["roster_text"]
    assert INITIAL_PW not in roster_payload["roster_csv"]
    assert "初始口令" not in roster_payload["roster_csv"]

    r_roster_bad = await client.get(
        f"/api/v1/admin/imports/{job_id}/roster?format=xml", headers=headers
    )
    assert r_roster_bad.status_code == 400
    assert r_roster_bad.json()["error"]["code"] == "invalid_format"

    # 4. Modify lab person & create lab project -> verify auto-reindex updates lab-profile-* immediately
    people_list = (await client.get("/api/v1/lab/people", headers=headers)).json()[
        "people"
    ]
    yang_person = next(p for p in people_list if p["name"] == "杨硕")
    r_patch_person = await client.patch(
        f"/api/v1/lab/people/{yang_person['id']}",
        json={"research_specialty": "知识图谱与大模型对齐评测"},
        headers=headers,
    )
    assert r_patch_person.status_code == 200, r_patch_person.text
    assert (
        r_patch_person.json()["person"]["research_specialty"]
        == "知识图谱与大模型对齐评测"
    )

    r_new_proj = await client.post(
        "/api/v1/lab/projects",
        json={
            "title": "教育智能体可信推理关键技术研究",
            "grant_no": "TJAI2026-99",
            "funder": "天津市自然科学基金重点项目",
            "pi_name": "杨硕",
            "lab_role": "host",
            "start_date": "2026-01-01",
            "status": "ongoing",
            "amount_wan": "30.00",
        },
        headers=headers,
    )
    assert r_new_proj.status_code == 200, r_new_proj.text
    new_proj_id = r_new_proj.json()["project"]["id"]
    rag_doc_key = r_new_proj.json()["rag_doc_key"]

    maker = get_sessionmaker()
    async with maker() as session:
        prof_doc = await session.scalar(
            select(Document).where(Document.doc_key == rag_doc_key)
        )
        assert prof_doc is not None
        assert "知识图谱与大模型对齐评测" in prof_doc.content_text
        assert "TJAI2026-99" in prof_doc.content_text
        prof_chunks = list(
            await session.scalars(
                select(Chunk).where(Chunk.document_id == prof_doc.id)
            )
        )
        joined_chunks = "\n".join(c.content for c in prof_chunks)
        assert "知识图谱与大模型对齐评测" in joined_chunks
        assert "TJAI2026-99" in joined_chunks

    # Chat stream immediately reflects newly added project TJAI2026-99 without manual reindex
    r_chat_new = await client.post(
        "/api/v1/chat/stream",
        json={"message": "课题组承担的 TJAI2026-99 项目由谁主持？"},
        headers=headers,
    )
    assert r_chat_new.status_code == 200
    chat_events = [
        json.loads(block.split("data:", 1)[1].strip())
        for block in r_chat_new.text.split("\n\n")
        if "data:" in block
    ]
    streamed_answer = "".join(
        str(e.get("text") or "") for e in chat_events if e.get("type") == "token"
    )
    assert "TJAI2026-99" in streamed_answer
    assert "杨硕" in streamed_answer

    # Delete the newly added project so counts return to 6
    r_del_proj = await client.delete(
        f"/api/v1/lab/projects/{new_proj_id}", headers=headers
    )
    assert r_del_proj.status_code == 200

    # Manual reindex endpoint works
    r_reindex = await client.post("/api/v1/lab/reindex", headers=headers)
    assert r_reindex.status_code == 200
    assert r_reindex.json()["status"] == "ok"

    # 5. Batch formatting (/api/v1/lab/format/batch)
    r_batch_proj = await client.post(
        "/api/v1/lab/format/batch",
        json={
            "tab": "projects",
            "numbered": True,
            "only_ongoing": True,
            "recent_years": 5,
            "include_relationship": True,
        },
        headers=headers,
    )
    assert r_batch_proj.status_code == 200, r_batch_proj.text
    bp_data = r_batch_proj.json()
    assert bp_data["count"] == 4
    assert bp_data["items"][0].startswith("[1] ")
    assert bp_data["items"][1].startswith("[2] ")
    assert "与本课题关系：" in bp_data["text"]

    r_batch_fac = await client.post(
        "/api/v1/lab/format/batch",
        json={"tab": "facilities", "numbered": True},
        headers=headers,
    )
    assert r_batch_fac.status_code == 200
    assert r_batch_fac.json()["count"] == 1
    assert r_batch_fac.json()["items"][0].startswith("[1] ")

    r_batch_ach = await client.post(
        "/api/v1/lab/format/batch",
        json={"tab": "achievements", "numbered": True, "with_support": False},
        headers=headers,
    )
    assert r_batch_ach.status_code == 200
    assert r_batch_ach.json()["count"] == 5
    assert "成果为大模型安全治理提供支撑" not in r_batch_ach.json()["text"]

    # 6. Export Markdown & Word (.docx)
    r_exp_md = await client.get("/api/v1/lab/export?format=md", headers=headers)
    assert r_exp_md.status_code == 200
    assert "text/markdown" in r_exp_md.headers["content-type"]
    md_text = r_exp_md.content.decode("utf-8")
    assert "AGE240005" in md_text
    assert_no_pii(md_text)

    r_exp_docx = await client.get("/api/v1/lab/export?format=docx", headers=headers)
    assert r_exp_docx.status_code == 200
    assert (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        in r_exp_docx.headers["content-type"]
    )
    exported_doc = docx.Document(io.BytesIO(r_exp_docx.content))
    doc_full_text = "\n".join(p.text for p in exported_doc.paragraphs)
    assert "万振凯" in doc_full_text
    assert "AGE240005" in doc_full_text
    assert_no_pii(doc_full_text)


async def test_codex_review_regressions(client: AsyncClient) -> None:
    """Regression suite covering all 5 Codex review scenarios."""
    admin_user = await _seed_admin()
    admin_headers = await _login_headers(client, ADMIN_EMAIL)
    raw_docx = build_synthetic_proposal_docx()

    # --- Regression 2: Manual assets created while an unprovisioned (`parsed`) import exists
    # must NOT attach to the deletable `parsed` import or be wiped when it is deleted ---
    r_parsed = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("unprovisioned.docx", raw_docx, "application/octet-stream")},
        headers=admin_headers,
    )
    assert r_parsed.status_code == 200
    parsed_job_id = r_parsed.json()["job_id"]

    # Explicitly linking a manual asset to an unprovisioned import must be rejected (409)
    r_bad_link = await client.post(
        "/api/v1/lab/projects",
        json={
            "title": "试图挂载未开号任务的项目",
            "grant_no": "BAD-LINK-01",
            "source_import_id": parsed_job_id,
        },
        headers=admin_headers,
    )
    assert r_bad_link.status_code == 409
    assert r_bad_link.json()["error"]["code"] == "import_not_provisioned"

    # Creating manual project, achievement, facility without explicit source_import_id creates
    # a safe `provisioned` placeholder job rather than attaching to `parsed_job_id`
    r_man_proj = await client.post(
        "/api/v1/lab/projects",
        json={
            "title": "手工独立重点项目",
            "grant_no": "MAN-PROJ-01",
            "funder": "国家自然科学基金",
            "pi_name": "万振凯",
            "status": "ongoing",
        },
        headers=admin_headers,
    )
    assert r_man_proj.status_code == 200, r_man_proj.text
    man_proj_id = r_man_proj.json()["project"]["id"]
    placeholder_doc_key = r_man_proj.json()["rag_doc_key"]
    assert r_man_proj.json()["project"]["source_import_id"] != parsed_job_id

    r_man_ach = await client.post(
        "/api/v1/lab/achievements",
        json={
            "kind": "paper",
            "title": "手工录入代表性论文",
            "first_author": "万振凯",
            "year": 2026,
        },
        headers=admin_headers,
    )
    assert r_man_ach.status_code == 200
    man_ach_id = r_man_ach.json()["achievement"]["id"]

    r_man_fac = await client.post(
        "/api/v1/lab/facilities",
        json={
            "category": "compute",
            "name": "手工录入A800推理集群",
            "spec": "8xA800-80GB",
        },
        headers=admin_headers,
    )
    assert r_man_fac.status_code == 200
    man_fac_id = r_man_fac.json()["facility"]["id"]

    # Deleting the unprovisioned `parsed` import job succeeds and leaves all manual assets intact
    r_del_unprov = await client.delete(
        f"/api/v1/admin/imports/{parsed_job_id}",
        headers=admin_headers,
    )
    assert r_del_unprov.status_code == 200
    projs_after = (
        await client.get("/api/v1/lab/projects", headers=admin_headers)
    ).json()["projects"]
    achs_after = (
        await client.get("/api/v1/lab/achievements", headers=admin_headers)
    ).json()["achievements"]
    facs_after = (
        await client.get("/api/v1/lab/facilities", headers=admin_headers)
    ).json()["facilities"]
    assert any(p["id"] == man_proj_id for p in projs_after)
    assert any(a["id"] == man_ach_id for a in achs_after)
    assert any(f["id"] == man_fac_id for f in facs_after)

    # --- Regression 3 & 4: Upload & provision a second import; verify stale `lab-profile-*`
    # documents are retired (`deleted_at IS NOT NULL`) and their chunks deleted ---
    r_imp2 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal2.docx", raw_docx, "application/octet-stream")},
        headers=admin_headers,
    )
    assert r_imp2.status_code == 200
    imp2_id = r_imp2.json()["job_id"]

    # Corrupt both a project title and an achievement title via PATCH /preview, then restore via reparse(fsm)
    prev2 = (
        await client.get(
            f"/api/v1/admin/imports/{imp2_id}/preview", headers=admin_headers
        )
    ).json()["preview"]
    orig_ach_title = prev2["achievements"][0]["title"]
    mod_achs = [dict(a) for a in prev2["achievements"]]
    mod_achs[0]["title"] = "被人为破坏的成果标题"
    await client.patch(
        f"/api/v1/admin/imports/{imp2_id}/preview",
        json={"achievements": mod_achs},
        headers=admin_headers,
    )
    r_rep_fsm = await client.post(
        f"/api/v1/admin/imports/{imp2_id}/reparse",
        json={"mode": "fsm"},
        headers=admin_headers,
    )
    assert r_rep_fsm.status_code == 200
    assert (
        r_rep_fsm.json()["preview"]["achievements"][0]["title"] == orig_ach_title
    )

    r_prov2 = await client.post(
        f"/api/v1/admin/imports/{imp2_id}/provision",
        json={"initial_password": INITIAL_PW},
        headers=admin_headers,
    )
    assert r_prov2.status_code == 200, r_prov2.text
    new_doc_key = r_prov2.json()["rag_doc_key"]
    assert new_doc_key != placeholder_doc_key

    maker = get_sessionmaker()
    async with maker() as session:
        active_profile_docs = list(
            await session.scalars(
                select(Document).where(
                    Document.doc_key.like("lab-profile-%"),
                    Document.deleted_at.is_(None),
                )
            )
        )
        assert len(active_profile_docs) == 1
        assert active_profile_docs[0].doc_key == new_doc_key

        old_doc = await session.scalar(
            select(Document).where(Document.doc_key == placeholder_doc_key)
        )
        assert old_doc is not None
        assert old_doc.deleted_at is not None
        old_chunks = list(
            await session.scalars(
                select(Chunk).where(Chunk.document_id == old_doc.id)
            )
        )
        assert len(old_chunks) == 0

    # --- Regression 1: Non-admin member cannot edit their own `name` or `project_role` (403),
    # cannot edit another person's project/achievement, and cannot transfer `pi_name` to someone else ---
    yang_init_headers = await _login_headers(client, "yangshuo", INITIAL_PW)
    yang_new_pw = "YangShuo-NewPw-2026!"
    r_chg = await client.post(
        "/api/v1/auth/change-password",
        json={"old_password": INITIAL_PW, "new_password": yang_new_pw},
        headers=yang_init_headers,
    )
    assert r_chg.status_code == 200, r_chg.text
    yang_headers = await _login_headers(client, "yangshuo", yang_new_pw)
    people_rows = (
        await client.get("/api/v1/lab/people", headers=yang_headers)
    ).json()["people"]
    yang_row = next(p for p in people_rows if p["name"] == "杨硕")
    orig_yang_fp = yang_row["entity_fingerprint"]

    # Member attempting to rename self to hijack ownership -> 403 forbidden
    r_hijack_name = await client.patch(
        f"/api/v1/lab/people/{yang_row['id']}",
        json={"name": "万振凯"},
        headers=yang_headers,
    )
    assert r_hijack_name.status_code == 403
    assert r_hijack_name.json()["error"]["code"] == "forbidden"

    # Member attempting to escalate project_role -> 403 forbidden
    r_hijack_role = await client.patch(
        f"/api/v1/lab/people/{yang_row['id']}",
        json={"project_role": "applicant"},
        headers=yang_headers,
    )
    assert r_hijack_role.status_code == 403
    assert r_hijack_role.json()["error"]["code"] == "forbidden"

    # Member updating own research_specialty and affiliation succeeds and recomputes entity_fingerprint
    r_self_ok = await client.patch(
        f"/api/v1/lab/people/{yang_row['id']}",
        json={
            "research_specialty": "可信知识图谱",
            "affiliation": "天津天狮学院人工智能系",
        },
        headers=yang_headers,
    )
    assert r_self_ok.status_code == 200, r_self_ok.text
    updated_yang = r_self_ok.json()["person"]
    assert updated_yang["research_specialty"] == "可信知识图谱"
    assert updated_yang["entity_fingerprint"] != orig_yang_fp

    # Member cannot modify Wan Zhenkai's project -> 403 forbidden
    r_other_proj = await client.patch(
        f"/api/v1/lab/projects/{man_proj_id}",
        json={"title": "越权修改他人项目"},
        headers=yang_headers,
    )
    assert r_other_proj.status_code == 403

    # Member creates own project, then tries to reassign pi_name to someone else -> 403 forbidden
    r_yang_proj = await client.post(
        "/api/v1/lab/projects",
        json={
            "title": "杨硕青年基金项目",
            "grant_no": "YS-2026-01",
            "funder": "天津市教委科研计划",
            "pi_name": "杨硕",
            "status": "ongoing",
        },
        headers=yang_headers,
    )
    assert r_yang_proj.status_code == 200
    yang_proj_id = r_yang_proj.json()["project"]["id"]
    orig_proj_fp = r_yang_proj.json()["project"]["entity_fingerprint"]

    r_reassign_pi = await client.patch(
        f"/api/v1/lab/projects/{yang_proj_id}",
        json={"pi_name": "万振凯"},
        headers=yang_headers,
    )
    assert r_reassign_pi.status_code == 403

    # --- Regression 5: Updating project identity fields recomputes `entity_fingerprint`,
    # and colliding with another existing project raises 409 duplicate_entity ---
    r_upd_proj = await client.patch(
        f"/api/v1/lab/projects/{yang_proj_id}",
        json={"title": "杨硕青年基金项目（升级版）", "grant_no": "YS-2026-02"},
        headers=yang_headers,
    )
    assert r_upd_proj.status_code == 200, r_upd_proj.text
    new_proj_fp = r_upd_proj.json()["project"]["entity_fingerprint"]
    assert new_proj_fp != orig_proj_fp

    # Colliding with `man_proj_id` ("手工独立重点项目", "MAN-PROJ-01", "国家自然科学基金") -> 409 duplicate_entity
    r_dup_proj = await client.patch(
        f"/api/v1/lab/projects/{yang_proj_id}",
        json={
            "title": "手工独立重点项目",
            "grant_no": "MAN-PROJ-01",
            "funder": "国家自然科学基金",
        },
        headers=admin_headers,
    )
    assert r_dup_proj.status_code == 409
    assert r_dup_proj.json()["error"]["code"] == "duplicate_entity"

    # --- Round 2 Regression A: Unowned project (`pi_name=None`) / achievement (`first_author=None`)
    # and shared facility (`PATCH/DELETE /lab/facilities/{id}`) cannot be modified/deleted by non-admin ---
    r_unowned_proj = await client.post(
        "/api/v1/lab/projects",
        json={"title": "未填写负责人的公共项目", "grant_no": "UNOWNED-01"},
        headers=admin_headers,
    )
    assert r_unowned_proj.status_code == 200
    unowned_proj_id = r_unowned_proj.json()["project"]["id"]

    r_claim_unowned = await client.patch(
        f"/api/v1/lab/projects/{unowned_proj_id}",
        json={"pi_name": "杨硕"},
        headers=yang_headers,
    )
    assert r_claim_unowned.status_code == 403
    assert r_claim_unowned.json()["error"]["code"] == "forbidden"

    r_del_unowned = await client.delete(
        f"/api/v1/lab/projects/{unowned_proj_id}",
        headers=yang_headers,
    )
    assert r_del_unowned.status_code == 403

    r_fac_member_patch = await client.patch(
        f"/api/v1/lab/facilities/{man_fac_id}",
        json={"name": "非管理员试图修改设备"},
        headers=yang_headers,
    )
    assert r_fac_member_patch.status_code == 403

    r_fac_member_del = await client.delete(
        f"/api/v1/lab/facilities/{man_fac_id}",
        headers=yang_headers,
    )
    assert r_fac_member_del.status_code == 403

    # --- Round 2 Regression B: Duplicate software/patent achievement creation returns 409 duplicate_entity ---
    r_sw1 = await client.post(
        "/api/v1/lab/achievements",
        json={
            "kind": "software",
            "title": "智能题库查重软著V1.0",
            "raw_text": "智能题库查重软著V1.0，登记号：2026SR888999",
            "first_author": "杨硕",
            "year": 2026,
        },
        headers=yang_headers,
    )
    assert r_sw1.status_code == 200
    r_sw_dup = await client.post(
        "/api/v1/lab/achievements",
        json={
            "kind": "software",
            "title": "智能题库查重软著V1.0（重复提交）",
            "raw_text": "智能题库查重软著V1.0，登记号：2026SR888999",
            "first_author": "杨硕",
            "year": 2026,
        },
        headers=yang_headers,
    )
    assert r_sw_dup.status_code == 409
    assert r_sw_dup.json()["error"]["code"] == "duplicate_entity"

    # --- Round 2 Regression C: Distinct `relationship_note` on a later project is preserved in RAG & export ---
    distinct_rel = "该青年项目为本课题提供可信知识图谱子图对齐算法原型"
    r_rel_upd = await client.patch(
        f"/api/v1/lab/projects/{yang_proj_id}",
        json={"relationship_note": distinct_rel},
        headers=yang_headers,
    )
    assert r_rel_upd.status_code == 200
    r_md_export = await client.get(
        "/api/v1/lab/export?format=md", headers=admin_headers
    )
    assert r_md_export.status_code == 200
    assert distinct_rel in r_md_export.content.decode("utf-8")

    # --- Round 3 & Round 4 Regression A: Member edits, /lab/reindex, and new import replacement
    # preserve canonical profile document owner_id, visibility, and DocumentACL grants ---
    async with maker() as session:
        prof_doc_before = await session.scalar(
            select(Document).where(Document.doc_key == new_doc_key)
        )
        assert prof_doc_before is not None
        assert prof_doc_before.owner_id == admin_user.id
        acl_group = Group(slug="profile-readers", name="档案授权阅读组")
        session.add(acl_group)
        await session.flush()
        acl_group_id = acl_group.id
        prof_doc_before.visibility = "private"
        session.add_all(
            [
                DocumentACL(
                    document_id=prof_doc_before.id,
                    principal_type="user",
                    principal_id= str(yang_row["user_id"]),
                    permission="read",
                ),
                DocumentACL(
                    document_id=prof_doc_before.id,
                    principal_type="group",
                    principal_id=str(acl_group_id),
                    permission="read",
                ),
                DocumentACL(
                    document_id=prof_doc_before.id,
                    principal_type="role",
                    principal_id="pi",
                    permission="read",
                ),
            ]
        )
        await session.commit()

    r_member_reindex = await client.post("/api/v1/lab/reindex", headers=yang_headers)
    assert r_member_reindex.status_code == 200
    async with maker() as session:
        prof_doc_after = await session.scalar(
            select(Document).where(Document.doc_key == new_doc_key)
        )
        assert prof_doc_after is not None
        assert prof_doc_after.owner_id == admin_user.id
        assert prof_doc_after.visibility == "private"
        preserved_acls = list(
            await session.scalars(
                select(DocumentACL).where(
                    DocumentACL.document_id == prof_doc_after.id
                )
            )
        )
        preserved_pairs = {
            (row.principal_type, row.principal_id) for row in preserved_acls
        }
        assert preserved_pairs == {
            ("user", str(yang_row["user_id"])),
            ("group", str(acl_group_id)),
            ("role", "pi"),
        }
        prof_doc_after.visibility = "lab"
        await session.commit()

    # --- Round 3 Regression B: T1 members retain certificate-based fingerprint across profile updates ---
    lin_row = next(p for p in people_rows if p["name"] == "林雪莹")
    wang_row = next(p for p in people_rows if p["name"] == "王诗涵")
    cao_row = next(p for p in people_rows if p["name"] == "曹梦轩")
    orig_lin_fp = lin_row["entity_fingerprint"]
    orig_wang_fp = wang_row["entity_fingerprint"]
    orig_cao_fp = cao_row["entity_fingerprint"]

    r_lin_upd = await client.patch(
        f"/api/v1/lab/people/{lin_row['id']}",
        json={"bio": "深度学习与教育大模型方向硕士生", "research_specialty": "大模型微调"},
        headers=admin_headers,
    )
    assert r_lin_upd.status_code == 200, r_lin_upd.text
    assert r_lin_upd.json()["person"]["entity_fingerprint"] == orig_lin_fp

    # Two T1 members with the same name and affiliation but distinct student IDs never collide on update
    r_cao_rename = await client.patch(
        f"/api/v1/lab/people/{cao_row['id']}",
        json={"name": "王诗涵", "bio": "同名T1研究生B"},
        headers=admin_headers,
    )
    assert r_cao_rename.status_code == 200, r_cao_rename.text
    assert r_cao_rename.json()["person"]["entity_fingerprint"] == orig_cao_fp

    r_wang_upd = await client.patch(
        f"/api/v1/lab/people/{wang_row['id']}",
        json={"bio": "同名T1研究生A"},
        headers=admin_headers,
    )
    assert r_wang_upd.status_code == 200, r_wang_upd.text
    assert r_wang_upd.json()["person"]["entity_fingerprint"] == orig_wang_fp

    # --- Round 5 Regression: Grouped profile sections > 800 chars split cleanly on blank paragraphs ---
    from app.importer.formatters import render_lab_profile_markdown
    from app.rag.chunker import chunk_markdown

    large_people = [
        {
            "name": f"成员{i}",
            "project_role": "applicant" if i == 0 else "member",
            "degree": "博士",
            "affiliation": "天津天狮学院计算机科学与技术学院",
            "title": "教授",
            "admin_duty": "院长",
            "tutor_role": "博士生导师",
            "research_specialty": "人工智能与大模型安全对齐研究",
            "bio": "长期从事人工智能、自然语言处理、教育测评大模型与多模态学习研究，主持多项国家级与省部级重点科研课题。",
        }
        for i in range(10)
    ]
    large_projects = [
        {
            "title": f"面向教育测评的大模型可信推理与安全对齐关键技术研究子课题{i}",
            "grant_no": f"AGE2026-{i:03d}",
            "funder": "全国教育科学规划重点课题",
            "pi_name": f"成员{i}",
            "lab_role": "host",
            "status": "ongoing" if i < 6 else "completed",
            "start_date": "2025-01-01",
            "amount_wan": "25.00",
        }
        for i in range(10)
    ]
    large_achievements = [
        {
            "kind": "paper",
            "title": f"大规模在线教育考试智能评卷与异常行为检测算法研究第{i}篇",
            "first_author": f"成员{i}",
            "venue": "计算机学报",
            "year": 2025,
            "support_note": "提出了高可靠试题结构化抽取与评分一致性校验方法",
        }
        for i in range(10)
    ]
    large_facilities = [
        {
            "name": f"高性能计算加速集群节点{i}",
            "spec": "8xA800-80GB GPU服务器及高速互联存储部件",
            "value_wan": "68.00",
            "usage_scenario": "用于课题组搭建教育考试专属大模型轻量化微调与推理验证实验环境",
        }
        for i in range(10)
    ]
    large_md = render_lab_profile_markdown(
        project_title="大型课题组档案切分回归测试",
        as_of_time="2026-06-01",
        people=large_people,
        projects=large_projects,
        achievements=large_achievements,
        facilities=large_facilities,
        relationship_note="本课题是在前期系列国家级重点课题基础上的深入拓展。",
    )
    assert len(large_md) > 4500
    large_chunks = chunk_markdown(large_md, max_chars=800)
    assert len(large_chunks) <= 30
    for ch in large_chunks:
        assert len(ch.text) <= 800, (
            f"Chunk {ch.index} ({ch.heading}) exceeded 800 chars: {len(ch.text)}"
        )
        first_para = ch.text.split("\n\n", 1)[0]
        if first_para.startswith("## "):
            assert "\n" not in first_para
    member9_chunks = [c for c in large_chunks if "成员9" in c.heading]
    assert len(member9_chunks) == 1
    assert "团队成员" not in member9_chunks[0].heading
    assert "成员8" not in member9_chunks[0].text
    proj9_chunks = [c for c in large_chunks if "AGE2026-009" in c.heading]
    assert len(proj9_chunks) == 1
    assert "承担项目" not in proj9_chunks[0].heading

    # --- Round 6 Regression A: Recompute inferred project status when end_date changes or is cleared ---
    r_inf_proj = await client.post(
        "/api/v1/lab/projects",
        json={
            "title": "自动推断状态测试课题",
            "grant_no": "INF-2026-01",
            "funder": "教育部人文社科项目",
            "pi_name": "杨硕",
            "start_date": "2023-01-01",
            "end_date": "2025-12-31",
            "amount_wan": "18.50",
            "status_source": "inferred",
            "as_of_time": "2026-06-01",
        },
        headers=yang_headers,
    )
    assert r_inf_proj.status_code == 200, r_inf_proj.text
    inf_proj = r_inf_proj.json()["project"]
    inf_proj_id = inf_proj["id"]
    assert inf_proj["status"] == "completed"
    assert inf_proj["status_source"] == "inferred"

    # Extending end_date past as_of_time recomputes inferred status to ongoing
    r_extend_end = await client.patch(
        f"/api/v1/lab/projects/{inf_proj_id}",
        json={"end_date": "2028-01-01"},
        headers=yang_headers,
    )
    assert r_extend_end.status_code == 200, r_extend_end.text
    assert r_extend_end.json()["project"]["end_date"] == "2028-01-01"
    assert r_extend_end.json()["project"]["status"] == "ongoing"
    assert r_extend_end.json()["project"]["status_source"] == "inferred"

    # Clearing end_date via explicit null sets end_date=None and recomputes inferred status to unknown
    r_clear_end = await client.patch(
        f"/api/v1/lab/projects/{inf_proj_id}",
        json={"end_date": None, "amount_wan": None, "start_date": None},
        headers=yang_headers,
    )
    assert r_clear_end.status_code == 200, r_clear_end.text
    assert r_clear_end.json()["project"]["end_date"] is None
    assert r_clear_end.json()["project"]["start_date"] is None
    assert r_clear_end.json()["project"]["amount_wan"] is None
    assert r_clear_end.json()["project"]["status"] == "unknown"

    # Non-admin member cannot clear pi_name to null -> 403 forbidden
    r_clear_pi_forbidden = await client.patch(
        f"/api/v1/lab/projects/{inf_proj_id}",
        json={"pi_name": None},
        headers=yang_headers,
    )
    assert r_clear_pi_forbidden.status_code == 403

    # --- Round 6 Regression B: Explicit null updates clear nullable fields across achievements, people, facilities ---
    sw1_id = r_sw1.json()["achievement"]["id"]
    assert r_sw1.json()["achievement"]["year"] == 2026

    # Non-admin member cannot clear first_author to null -> 403 forbidden
    r_clear_author_forbidden = await client.patch(
        f"/api/v1/lab/achievements/{sw1_id}",
        json={"first_author": None},
        headers=yang_headers,
    )
    assert r_clear_author_forbidden.status_code == 403

    # Clearing achievement year via explicit null sets year=None
    r_clear_year = await client.patch(
        f"/api/v1/lab/achievements/{sw1_id}",
        json={"year": None, "venue": None, "support_note": None},
        headers=yang_headers,
    )
    assert r_clear_year.status_code == 200, r_clear_year.text
    assert r_clear_year.json()["achievement"]["year"] is None
    assert r_clear_year.json()["achievement"]["venue"] is None
    assert r_clear_year.json()["achievement"]["support_note"] is None

    # Clearing person nullable fields via explicit null
    r_clear_person = await client.patch(
        f"/api/v1/lab/people/{yang_row['id']}",
        json={"research_specialty": None, "bio": None},
        headers=yang_headers,
    )
    assert r_clear_person.status_code == 200, r_clear_person.text
    assert r_clear_person.json()["person"]["research_specialty"] is None
    assert r_clear_person.json()["person"]["bio"] is None

    # Clearing facility nullable fields via explicit null
    r_clear_fac = await client.patch(
        f"/api/v1/lab/facilities/{man_fac_id}",
        json={"spec": None, "value_wan": None, "usage_scenario": None},
        headers=admin_headers,
    )
    assert r_clear_fac.status_code == 200, r_clear_fac.text
    assert r_clear_fac.json()["facility"]["spec"] is None
    assert r_clear_fac.json()["facility"]["value_wan"] is None
    assert r_clear_fac.json()["facility"]["usage_scenario"] is None


async def test_cross_tier_member_deduplication_when_second_proposal_omits_student_ids(
    client: AsyncClient,
) -> None:
    """Regression guard: importing a second proposal without student IDs must link existing T1 members instead of duplicating them as T2."""
    await _seed_admin()
    headers = await _login_headers(client, ADMIN_EMAIL)

    # 1. First import: members 林雪莹 (2431101262), 王诗涵 (2431101226), 曹梦轩 (2431111321) have T1 student IDs
    raw_docx_1 = build_synthetic_proposal_docx()
    r_up1 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={
            "file": (
                "first_with_student_ids.docx",
                raw_docx_1,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers=headers,
    )
    assert r_up1.status_code == 200, r_up1.text
    job1_id = r_up1.json()["job_id"]

    r_exec1 = await client.post(
        f"/api/v1/admin/imports/{job1_id}/provision",
        json={"initial_password": INITIAL_PW},
        headers=headers,
    )
    assert r_exec1.status_code == 200, r_exec1.text

    # 2. Second import: same members WITHOUT student IDs (empty cert), plus two new members 徐子晗 and 米琦
    doc2 = docx.Document(io.BytesIO(raw_docx_1))
    t2 = doc2.tables[1]
    # Clear student IDs on rows 11, 12, 13 (林雪莹, 王诗涵, 曹梦轩)
    for row_idx in (11, 12, 13):
        t2.rows[row_idx].cells[2].text = ""
    # Append two new members without student IDs: 徐子晗, 米琦
    for seq, name, spec in [("10", "徐子晗", "智能评测"), ("11", "米琦", "教育数据挖掘")]:
        new_row = t2.add_row()
        vals = [seq, name, "", "无", "无", "无", spec, "学士", "天津工业大学", ""]
        for c_idx, val in enumerate(vals):
            new_row.cells[c_idx].text = val

    buf2 = io.BytesIO()
    doc2.save(buf2)
    raw_docx_2 = buf2.getvalue()

    r_up2 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={
            "file": (
                "second_without_student_ids.docx",
                raw_docx_2,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers=headers,
    )
    assert r_up2.status_code == 200, r_up2.text
    preview2_people = {p["name"]: p for p in r_up2.json()["preview"]["people"] if p["affiliation"] == "天津工业大学"}
    assert preview2_people["林雪莹"]["match"]["status"] == "existing"
    assert preview2_people["林雪莹"]["login_name"] == "2431101262"
    assert preview2_people["王诗涵"]["match"]["status"] == "existing"
    assert preview2_people["王诗涵"]["login_name"] == "2431101226"
    assert preview2_people["曹梦轩"]["match"]["status"] == "existing"
    assert preview2_people["曹梦轩"]["login_name"] == "2431111321"
    assert preview2_people["徐子晗"]["match"]["status"] == "new"
    assert preview2_people["徐子晗"]["login_name"] == "xuzihan"
    assert preview2_people["米琦"]["match"]["status"] == "new"
    assert preview2_people["米琦"]["login_name"] == "miqi"

    job2_id = r_up2.json()["job_id"]
    r_exec2 = await client.post(
        f"/api/v1/admin/imports/{job2_id}/provision",
        json={"initial_password": INITIAL_PW},
        headers=headers,
    )
    assert r_exec2.status_code == 200, r_exec2.text
    exec2_stats = r_exec2.json()["stats"]
    assert exec2_stats["users_created"] == 2
    assert exec2_stats["users_linked"] == 10
    roster_rows2 = exec2_stats["roster_rows"]
    created_names2 = {
        r["name"] for r in roster_rows2 if r["account_status"] == "new"
    }
    assert created_names2 == {"徐子晗", "米琦"}
    existing_by_name2 = {
        r["name"]: r for r in roster_rows2 if r["account_status"] == "existing"
    }
    assert existing_by_name2["林雪莹"]["login_name"] == "2431101262"
    assert existing_by_name2["王诗涵"]["login_name"] == "2431101226"
    assert existing_by_name2["曹梦轩"]["login_name"] == "2431111321"

    r_roster2 = await client.get(f"/api/v1/admin/imports/{job2_id}/roster", headers=headers)
    assert r_roster2.status_code == 200, r_roster2.text
    roster_text2 = r_roster2.json()["roster_text"]
    assert "徐子晗　登录名：xuzihan" in roster_text2
    assert "米琦　登录名：miqi" in roster_text2
    assert "linxueying" not in roster_text2
    assert "wangshihan" not in roster_text2
    assert "caomengxuan" not in roster_text2

    r_people = await client.get("/api/v1/lab/people", headers=headers)
    assert r_people.status_code == 200, r_people.text
    people_items = r_people.json()["people"]
    maker = get_sessionmaker()
    async with maker() as session:
        all_users = {str(u.id): u for u in await session.scalars(select(User))}
    for student_name, expected_login in [
        ("林雪莹", "2431101262"),
        ("王诗涵", "2431101226"),
        ("曹梦轩", "2431111321"),
    ]:
        matches = [p for p in people_items if p["name"] == student_name]
        assert len(matches) == 1
        assert matches[0]["user_id"] in all_users
        assert all_users[matches[0]["user_id"]].login_name == expected_login

    # 3. Third import (T2 -> T1): 徐子晗 was created as T2 (xuzihan), now imported with student ID 2531108888
    doc3 = docx.Document(io.BytesIO(raw_docx_2))
    t3 = doc3.tables[1]
    xuzihan_row = next(r for r in t3.rows if r.cells[1].text == "徐子晗")
    xuzihan_row.cells[2].text = "2531108888"
    buf3 = io.BytesIO()
    doc3.save(buf3)

    r_up3 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={
            "file": (
                "third_with_xuzihan_student_id.docx",
                buf3.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers=headers,
    )
    assert r_up3.status_code == 200, r_up3.text
    preview3_people = {p["name"]: p for p in r_up3.json()["preview"]["people"] if p["affiliation"] == "天津工业大学"}
    assert preview3_people["徐子晗"]["match"]["status"] == "existing"
    assert preview3_people["徐子晗"]["login_name"] == "xuzihan"

    job3_id = r_up3.json()["job_id"]
    r_exec3 = await client.post(
        f"/api/v1/admin/imports/{job3_id}/provision",
        json={"initial_password": INITIAL_PW},
        headers=headers,
    )
    assert r_exec3.status_code == 200, r_exec3.text

    # 3b. Fourth import: homonym 徐子晗 with a DIFFERENT T1 student ID (2531109999) must NOT link to xuzihan (2531108888)
    doc4 = docx.Document(io.BytesIO(raw_docx_2))
    t4 = doc4.tables[1]
    xuzihan_row4 = next(r for r in t4.rows if r.cells[1].text == "徐子晗")
    xuzihan_row4.cells[2].text = "2531109999"
    buf4 = io.BytesIO()
    doc4.save(buf4)

    r_up4 = await client.post(
        "/api/v1/admin/imports/proposal",
        files={
            "file": (
                "fourth_with_homonym_xuzihan.docx",
                buf4.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers=headers,
    )
    assert r_up4.status_code == 200, r_up4.text
    preview4_people = {p["name"]: p for p in r_up4.json()["preview"]["people"] if p["affiliation"] == "天津工业大学"}
    assert preview4_people["徐子晗"]["match"]["status"] == "new"
    assert preview4_people["徐子晗"]["login_name"] == "2531109999"

    # 4. Ambiguity guard: if an unlinked LabPerson with the same (name, affiliation) also exists,
    # an incoming T2 row must NOT auto-link to the linked person.
    import uuid as _uuid

    from app.importer.fingerprint import person_fp
    from app.importer.people import assign_people_identities
    from app.importer.provenance import _match_existing_person
    from app.models import LabPerson

    u_linked = User(
        id=_uuid.uuid4(),
        login_name="2431101262",
        display_name="林雪莹",
        role="member",
        password_hash="x",
        is_active=True,
    )
    lp_linked = LabPerson(
        id=_uuid.uuid4(),
        user_id=u_linked.id,
        name="林雪莹",
        affiliation="天津工业大学",
        entity_fingerprint=person_fp(
            tier="T1",
            login_name="2431101262",
            name="林雪莹",
            affiliation="天津工业大学",
        ),
    )
    lp_unlinked = LabPerson(
        id=_uuid.uuid4(),
        user_id=None,
        name="林雪莹",
        affiliation="天津工业大学",
        entity_fingerprint=person_fp(
            tier="T1",
            login_name="2531109999",
            name="林雪莹",
            affiliation="天津工业大学",
        ),
    )
    inc_ambiguous = [{"name": "林雪莹", "affiliation": "天津工业大学"}]
    assign_people_identities(
        inc_ambiguous,
        existing_users=[u_linked],
        existing_people=[lp_linked, lp_unlinked],
    )
    assert inc_ambiguous[0]["match"]["status"] == "new"  # type: ignore[index]

    # 5. Conflicting affiliation guard in _match_existing_person:
    # An existing T2 person in affiliation A must not match an incoming T1 person in affiliation B.
    lp_t2_univ_a = LabPerson(
        id=_uuid.uuid4(),
        user_id=_uuid.uuid4(),
        name="单立飞",
        affiliation="天津天狮学院",
        entity_fingerprint=person_fp(
            tier="T2",
            login_name="shanlifei",
            name="单立飞",
            affiliation="天津天狮学院",
        ),
    )
    inc_t1_univ_b = {
        "name": "单立飞",
        "tier": "T1",
        "login_name": "2531107777",
        "affiliation": "北京大学",
        "match": {"status": "new"},
    }
    inc_t1_fp = person_fp(
        tier="T1",
        login_name="2531107777",
        name="单立飞",
        affiliation="北京大学",
    )
    matched_conflicting_aff = _match_existing_person(
        inc_t1_univ_b,
        "单立飞",
        inc_t1_fp,
        {"affiliation": "北京大学", "email": None},
        [lp_t2_univ_a],
        {"单立飞": 1},
    )
    assert matched_conflicting_aff is None

    # 6. Suffixed pinyin login (e.g. zhangsan2, matching T1_CERT_RE) that learns a T1 student ID
    # must retain student_id across preview re-evaluations and reject a homonym with a different T1 student ID.
    u_suffixed = User(
        id=_uuid.uuid4(),
        login_name="zhangsan2",
        display_name="张三",
        role="member",
        password_hash="x",
        is_active=True,
    )
    lp_suffixed_t2 = LabPerson(
        id=_uuid.uuid4(),
        user_id=u_suffixed.id,
        name="张三",
        affiliation="天津工业大学",
        entity_fingerprint=person_fp(
            tier="T2",
            login_name="zhangsan2",
            name="张三",
            affiliation="天津工业大学",
        ),
    )
    inc_learn_t1 = [
        {"name": "张三", "affiliation": "天津工业大学", "cert": "2531108888"}
    ]
    assign_people_identities(
        inc_learn_t1,
        existing_users=[u_suffixed],
        existing_people=[lp_suffixed_t2],
    )
    assert inc_learn_t1[0]["login_name"] == "zhangsan2"
    assert inc_learn_t1[0]["student_id"] == "2531108888"
    # Simulate preview-update handler re-evaluating the roster item
    inc_learn_t1[0]["cert"] = (
        inc_learn_t1[0].get("student_id") or inc_learn_t1[0].get("login_name")
    )
    assign_people_identities(
        inc_learn_t1,
        existing_users=[u_suffixed],
        existing_people=[lp_suffixed_t2],
    )
    assert inc_learn_t1[0]["student_id"] == "2531108888"
    assert inc_learn_t1[0]["fingerprint"] == person_fp(
        tier="T1",
        login_name="2531108888",
        name="张三",
        affiliation="天津工业大学",
    )

    lp_suffixed_learned_t1 = LabPerson(
        id=lp_suffixed_t2.id,
        user_id=u_suffixed.id,
        name="张三",
        affiliation="天津工业大学",
        entity_fingerprint=inc_learn_t1[0]["fingerprint"],
    )
    inc_conflicting_t1 = [
        {"name": "张三", "affiliation": "天津工业大学", "cert": "2531109999"}
    ]
    assign_people_identities(
        inc_conflicting_t1,
        existing_users=[u_suffixed],
        existing_people=[lp_suffixed_learned_t1],
    )
    assert inc_conflicting_t1[0]["match"]["status"] == "new"  # type: ignore[index]
    assert inc_conflicting_t1[0]["login_name"] == "2531109999"

    # 7. Excluded row preceding an included row must not consume the existing user match.
    inc_excluded_then_included = [
        {
            "name": "张三",
            "affiliation": "天津工业大学",
            "cert": "2531108888",
            "exclude": True,
        },
        {
            "name": "张三",
            "affiliation": "天津工业大学",
        },
    ]
    assign_people_identities(
        inc_excluded_then_included,
        existing_users=[u_suffixed],
        existing_people=[lp_suffixed_t2],
    )
    assert inc_excluded_then_included[0]["match"]["status"] == "new"  # type: ignore[index]
    assert inc_excluded_then_included[1]["match"]["status"] == "existing"  # type: ignore[index]
    assert inc_excluded_then_included[1]["match"]["user_id"] == str(u_suffixed.id)  # type: ignore[index]
    assert inc_excluded_then_included[1]["login_name"] == "zhangsan2"









