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
from app.models import User
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
    assert res2["stats"]["people_skipped"] == 10
    assert res2["stats"]["projects_skipped"] == 6
    assert res2["stats"]["achievements_skipped"] == 5
    assert res2["stats"]["facilities_skipped"] == 1


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
    t1_token = r_t1.json()["access_token"]
    t1_headers = {"Authorization": f"Bearer {t1_token}"}
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

    achs = (
        await client.get("/api/v1/lab/achievements?kind=report", headers=headers)
    ).json()["achievements"]
    assert len(achs) == 1
    assert "研究报告/资政报告" in achs[0]["application_text"]

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
