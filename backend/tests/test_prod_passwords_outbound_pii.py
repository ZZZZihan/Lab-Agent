"""Regression tests for prod password guards, CSV injection escaping, minority/Latin naming, Word/RTF security, LLM reparse, and outbound PII enforcement."""

from __future__ import annotations

import base64
import io
import json
import uuid
import zipfile
from datetime import date
from typing import Any

import docx
import httpx
import pytest
import striprtf.striprtf as _striprtf
from app.auth import hash_password, validate_password
from app.config import Settings
from app.db import get_sessionmaker
from app.importer.llm_reparse import LLMReparseError, reparse_preview_payload_with_llm
from app.importer.parser import _extract_facilities, parse_proposal_docx
from app.importer.people import (
    assign_people_identities,
    name_to_pinyin,
    normalize_person_name,
)
from app.importer.pii import escape_csv_cell
from app.importer.reader import (
    DocxValidationError,
    extract_word_text,
    validate_and_load_docx,
)
from app.models import AuditEvent, LabAchievement, LabPerson, LabProject, User
from app.rag.extract import extract_upload
from httpx import AsyncClient
from sqlalchemy import select, text

ADMIN_EMAIL = "admin@lab.local"
ADMIN_PASSWORD = "LabAgent-Test-Password-2026"
DEFAULT_IMPORT_PW = "LabAgent@2026"
STRONG_NEW_PW = "Qz9!vK4#mP2@xL8$nR5%"
TEST_PHONE = "13812345678"
TEST_ID18 = "110101199001010000"

# 12x12 valid PNG image for signature upload/download tests
PNG_12X12_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAwAAAAMCAYAAABWdVznAAAAFUlEQVR4nGP8z8DAwMjAwwBTwAgEAACV"
    "AgP5S2z2AAAAAElFTkSuQmCC"
)


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


def _build_proposal_docx_with_names(
    *,
    applicant_name: str = "万振凯",
    member_rows: list[list[str]] | None = None,
    budget_rows: list[list[str]] | None = None,
) -> bytes:
    doc = docx.Document()
    t1 = doc.add_table(rows=5, cols=2)
    for idx, (k, v) in enumerate(
        [
            ("项 目 名 称", "多语种跨模态教育测评关键技术研究"),
            ("申 请 人", applicant_name),
            ("所 在 单 位", "天津天狮学院"),
            ("通 讯 地 址", "天津市武清区"),
            ("填 表 日 期", "2026 年 3 月 1 日"),
        ]
    ):
        t1.rows[idx].cells[0].text = k
        t1.rows[idx].cells[1].text = v

    members = member_rows or []
    t2 = doc.add_table(rows=5 + len(members), cols=10)
    t2.rows[0].cells[0].text = "申请人基本情况"
    for c_idx, val in enumerate(
        [
            "姓名",
            applicant_name,
            "性别",
            "男",
            "出生年月",
            "197001",
            "专业技术职务",
            "教授",
            "行政职务",
            "",
        ]
    ):
        t2.rows[1].cells[c_idx].text = val
    for c_idx, val in enumerate(
        [
            "最终学位",
            "博士",
            "研究专长",
            "智能评卷",
            "担任导师",
            "博士生导师",
            "所在单位",
            "天津天狮学院",
            "电子信箱",
            "",
        ]
    ):
        t2.rows[2].cells[c_idx].text = val
    for c_idx, val in enumerate(
        ["证件类型", "身份证", "证件号码", TEST_ID18, "联系电话", TEST_PHONE, "", "", "", ""]
    ):
        t2.rows[3].cells[c_idx].text = val
    for c_idx, val in enumerate(
        [
            "序号",
            "姓名",
            "性别",
            "出生年月",
            "专业技术职务",
            "行政职务",
            "研究专长",
            "最终学位",
            "所在单位",
            "证件号码",
        ]
    ):
        t2.rows[4].cells[c_idx].text = val
    for m_idx, m_row in enumerate(members, start=5):
        for c_idx, val in enumerate(m_row[:10]):
            t2.rows[m_idx].cells[c_idx].text = val

    t3 = doc.add_table(rows=1, cols=1)
    t3.rows[0].cells[0].text = (
        "前期研究基础\n"
        f"1．[学术简历] {applicant_name}，联系电话 {TEST_PHONE}，天津天狮学院教授。\n"
        "2．[前期成果]\n"
        f"（1）{applicant_name}，《多语种知识追踪模型》，计算机学报，2025年。\n"
        "3．[承担项目]\n"
        f"（1）《智能评测基础平台》（AGE240005），{applicant_name}（主持），全国教育科学规划课题，2024.01—2025.12，经费 120 万元。"
    )

    if budget_rows:
        tb = doc.add_table(rows=len(budget_rows), cols=max(len(r) for r in budget_rows))
        for r_idx, row_vals in enumerate(budget_rows):
            for c_idx, val in enumerate(row_vals):
                tb.rows[r_idx].cells[c_idx].text = val

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


STRONG_SECRET_A = "aB1cD2eF3gH4iJ5kL6mN7oP8qR9sT0uV_wX-yZ!"
STRONG_SECRET_B = "pQ9rS8tU7vW6xY5zA4bC3dE2fG1hI0jK_lM-nO!"
STRONG_SECRET_C = "mN1bV2cX3zL4kJ5hG6fD7sA8pO9iU0yT_rE-wQ!"


def test_validate_password_prod_rejects_import_default_password() -> None:
    dev_settings = Settings(
        app_env="dev",
        llm_capture=False,
        jwt_secret=STRONG_SECRET_A,
        audit_hmac_key=STRONG_SECRET_C,
    )
    prod_settings = Settings(
        app_env="prod",
        llm_capture=False,
        jwt_secret=STRONG_SECRET_A,
        internal_proxy_secret=STRONG_SECRET_B,
        audit_hmac_key=STRONG_SECRET_C,
        cors_origins="https://lab.example.edu",
    )
    validate_password(DEFAULT_IMPORT_PW, settings=dev_settings)
    with pytest.raises(ValueError, match="默认公开初始口令"):
        validate_password(DEFAULT_IMPORT_PW, settings=prod_settings)
    validate_password(STRONG_NEW_PW, settings=prod_settings)


async def test_prod_routes_reject_default_password_and_require_provision_password(
    client: AsyncClient,
) -> None:
    _, headers = await _seed_admin_and_login(client)

    docx_bytes = _build_proposal_docx_with_names()
    up_resp = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=headers,
        files={
            "file": (
                "prod-check.docx",
                docx_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert up_resp.status_code == 200, up_resp.text
    job_id = up_resp.json()["job_id"]

    app_settings: Settings = client._transport.app.state.settings  # type: ignore[attr-defined]
    orig_env = app_settings.app_env
    app_settings.app_env = "prod"
    try:
        # 1. Admin create_user rejects import_default_password in prod
        r_create = await client.post(
            "/api/v1/admin/users",
            headers=headers,
            json={
                "email": "limingyuan@lab.edu.cn",
                "display_name": "测试成员",
                "role": "member",
                "password": DEFAULT_IMPORT_PW,
            },
        )
        assert r_create.status_code == 400
        assert r_create.json()["error"]["code"] == "weak_password"

        # 2. Admin create_user validates email format and display_name length
        r_bad_email = await client.post(
            "/api/v1/admin/users",
            headers=headers,
            json={
                "email": "not-an-email",
                "display_name": "测试成员",
                "role": "member",
                "password": STRONG_NEW_PW,
            },
        )
        assert r_bad_email.status_code == 400
        assert r_bad_email.json()["error"]["code"] == "invalid_email"

        r_long_name = await client.post(
            "/api/v1/admin/users",
            headers=headers,
            json={
                "email": "limingyuan@lab.edu.cn",
                "display_name": "张" * 121,
                "role": "member",
                "password": STRONG_NEW_PW,
            },
        )
        assert r_long_name.status_code == 400
        assert r_long_name.json()["error"]["code"] == "invalid_display_name"

        # 3. Provision in prod without initial_password fails with 422 initial_password_required
        r_prov_missing = await client.post(
            f"/api/v1/admin/imports/{job_id}/provision",
            headers=headers,
            json={},
        )
        assert r_prov_missing.status_code == 422
        assert r_prov_missing.json()["error"]["code"] == "initial_password_required"

        # 4. Provision in prod with DEFAULT_IMPORT_PW fails with 422 password_policy_violation
        r_prov_default = await client.post(
            f"/api/v1/admin/imports/{job_id}/provision",
            headers=headers,
            json={"initial_password": DEFAULT_IMPORT_PW},
        )
        assert r_prov_default.status_code == 422
        assert r_prov_default.json()["error"]["code"] == "password_policy_violation"

        # 5. Provision in prod with strong password succeeds
        r_prov_ok = await client.post(
            f"/api/v1/admin/imports/{job_id}/provision",
            headers=headers,
            json={"initial_password": STRONG_NEW_PW},
        )
        assert r_prov_ok.status_code == 200, r_prov_ok.text

        # 6. Change-password rejects DEFAULT_IMPORT_PW in prod
        r_chg = await client.post(
            "/api/v1/auth/change-password",
            headers=headers,
            json={
                "old_password": ADMIN_PASSWORD,
                "new_password": DEFAULT_IMPORT_PW,
            },
        )
        assert r_chg.status_code == 400
        assert r_chg.json()["error"]["code"] == "invalid_password"
    finally:
        app_settings.app_env = orig_env


def test_escape_csv_cell_handles_leading_whitespace_and_control_prefixes() -> None:
    assert escape_csv_cell("normal") == "normal"
    assert escape_csv_cell("=CMD|' /C calc'!A0") == "'=CMD|' /C calc'!A0"
    assert escape_csv_cell("  =SUM(A1:A2)") == "'  =SUM(A1:A2)"
    assert escape_csv_cell("\x01+1+1") == "'\x01+1+1"
    assert escape_csv_cell(" -2+3") == "' -2+3"
    assert escape_csv_cell("\t@IMPORTXML()") == "'\t@IMPORTXML()"
    assert escape_csv_cell(" |cmd") == "' |cmd"


def test_minority_and_latin_person_names_and_generated_suffixed_logins() -> None:
    # Middle-dot variants & Latin word spacing
    assert (
        normalize_person_name("阿不都热合曼•买买提明・艾尔肯")
        == "阿不都热合曼·买买提明·艾尔肯"
    )
    assert normalize_person_name("Alice   Marie   Smith") == "Alice Marie Smith"
    assert normalize_person_name("张   三") == "张三"

    latin_base = name_to_pinyin("Alice Marie Smith")
    assert latin_base == "alicemariesmith"

    long_minority = "阿不都热合曼·买买提明·艾尔肯·依布拉音·买买提"
    long_base = name_to_pinyin(long_minority)
    assert 1 <= len(long_base) <= 56
    assert long_base.isalpha() and long_base.islower()

    # Digit-containing edge inputs never leak trailing digits into base_pinyin
    no_digit_base = name_to_pinyin("John2 Doe3")
    assert no_digit_base == "johndoe"

    # Assign identities across duplicate Latin & long minority names -> suffixed logins <= 64 chars
    people_rows: list[dict[str, Any]] = [
        {"name": long_minority, "affiliation": "甲校", "project_role": "applicant"},
        {"name": long_minority, "affiliation": "乙校", "project_role": "member"},
        {"name": "Alice Marie Smith", "affiliation": "甲校", "project_role": "member"},
        {"name": "Alice Marie Smith", "affiliation": "乙校", "project_role": "member"},
    ]
    assign_people_identities(people_rows, existing_users=[], existing_people=[])
    logins = [str(r["login_name"]) for r in people_rows]
    assert logins[0] == long_base
    assert logins[1] == f"{long_base}2"
    assert all(len(lg) <= 64 for lg in logins)
    assert logins[2] == "alicemariesmith"
    assert logins[3] == "alicemariesmith2"

    # Accented Unicode Latin & rare CJK Extension A/B names
    assert normalize_person_name("José   García") == "José García"
    assert name_to_pinyin("José García") == "josegarcia"
    assert normalize_person_name("龚 䶮") == "龚䶮"
    assert name_to_pinyin("龚䶮") == "gongyan"

    intl_docx = _build_proposal_docx_with_names(
        applicant_name="José García",
        member_rows=[
            [
                "1",
                "龚䶮",
                "男",
                "199103",
                "副教授",
                "",
                "计算语言学",
                "博士",
                "天津天狮学院",
                "",
            ],
            [
                "2",
                "王𠮷",
                "女",
                "199406",
                "讲师",
                "",
                "智能评测",
                "博士",
                "天津天狮学院",
                "",
            ],
        ],
    )
    parsed_intl = parse_proposal_docx("intl_proposal.docx", intl_docx)
    intl_people = parsed_intl["preview"]["people"]
    assert [p["name"] for p in intl_people] == ["José García", "龚䶮", "王𠮷"]
    assert intl_people[0]["project_role"] == "applicant"
    assert intl_people[0]["login_name"] == "josegarcia"
    assert intl_people[1]["login_name"] == "gongyan"


def test_parser_minority_names_and_equipment_budget_row_sequence_guard() -> None:
    minority_name = "阿不都热合曼•买买提明·艾尔肯"
    docx_bytes = _build_proposal_docx_with_names(
        applicant_name="万振凯",
        member_rows=[
            [
                "1",
                minority_name,
                "男",
                "199205",
                "讲师",
                "",
                "多语种自然语言处理",
                "博士",
                "天津天狮学院",
                "20260001",
            ],
            [
                "2",
                "Alice Smith",
                "女",
                "199008",
                "副教授",
                "",
                "教育测量学",
                "博士",
                "天津天狮学院",
                "",
            ],
        ],
        budget_rows=[
            ["序号", "科目名称", "金额（万元）", "测算依据"],
            # Sequence number "3" BEFORE "设备费", real amount "0" -> must NOT parse 3.00 万元!
            ["3", "设备费", "0", "无新购设备计划"],
        ],
    )
    parsed = parse_proposal_docx("proposal.docx", docx_bytes)
    preview = parsed["preview"]
    names = [p["name"] for p in preview["people"]]
    assert "阿不都热合曼·买买提明·艾尔肯" in names
    assert "Alice Smith" in names
    # Sequence number "3" was ignored and zero-amount equipment row was not fabricated
    assert preview["facilities"] == []

    # Verify positive amount with "万元" suffix and custom calculation basis after "设备费"
    facilities, _ = _extract_facilities(
        [
            [
                ["序号", "预算科目", "预算经费", "购置说明"],
                ["3", "设备费", "48.5万元", "多模态眼动实验采集阵列"],
            ]
        ]
    )
    assert len(facilities) == 1
    assert facilities[0]["value_wan"] == "48.50"
    assert facilities[0]["name"] == "多模态眼动实验采集阵列"


def test_docx_xxe_in_secondary_xml_and_rels_rejected_and_rtf_globals_unmodified() -> None:
    valid_bytes = _build_proposal_docx_with_names()
    src_zf = zipfile.ZipFile(io.BytesIO(valid_bytes), "r")
    tampered_buf = io.BytesIO()
    xxe_rels = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<!DOCTYPE Relationships [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'
    )
    with zipfile.ZipFile(tampered_buf, "w", compression=zipfile.ZIP_DEFLATED) as out_zf:
        for info in src_zf.infolist():
            if info.filename == "word/_rels/document.xml.rels":
                out_zf.writestr(info.filename, xxe_rels)
            else:
                out_zf.writestr(info.filename, src_zf.read(info.filename))

    with pytest.raises(DocxValidationError) as exc_info:
        validate_and_load_docx("evil.docx", tampered_buf.getvalue())
    assert exc_info.value.code == "import_unsupported_format"
    assert "XML 实体" in exc_info.value.message

    # RTF extraction preserves striprtf module globals and extracts CJK cleanly
    orig_destinations = set(_striprtf.destinations)
    orig_special = dict(_striprtf.specialchars)
    bs = chr(92)
    rtf_payload = (
        f"{{{bs}rtf1{bs}ansi{bs}ansicpg936 {{{bs}fonttbl{{{bs}f0 SimSun;}}}}{bs}pard "
        f"{bs}u26234?{bs}u-32515?{bs}u-29756?{bs}u27979?研究方案{bs}par}}"
    ).encode("gb18030")
    extracted_rtf = extract_word_text("plan.doc", rtf_payload)
    assert "智能评测研究方案" in extracted_rtf
    assert set(_striprtf.destinations) == orig_destinations
    assert dict(_striprtf.specialchars) == orig_special


def test_rag_extract_upload_scrubs_pii_in_docx() -> None:
    doc = docx.Document()
    doc.add_paragraph(
        f"项目联系人电话：{TEST_PHONE}，身份证号：{TEST_ID18}，研究方向为多模态评测。"
    )
    buf = io.BytesIO()
    doc.save(buf)
    res = extract_upload("handbook.docx", buf.getvalue(), max_chars=50_000)
    assert TEST_PHONE not in res.text
    assert TEST_ID18 not in res.text
    assert "[已脱敏]" in res.text


async def test_llm_reparse_scrubs_outbound_chunks_and_parses_structured_json() -> None:
    captured_prompts: list[str] = []

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        req_body = json.loads(request.content.decode())
        captured_prompts.append(req_body["messages"][-1]["content"])
        content_obj = {
            "achievements": [
                {
                    "index": 1,
                    "kind": "paper",
                    "title": "跨模态语义对齐评测方法",
                    "first_author": "万振凯",
                    "authors_text": "万振凯, 李明",
                    "venue": "软件学报",
                    "year": 2025,
                    "support_note": f"联系方式 {TEST_PHONE}",
                }
            ],
            "projects": [
                {
                    "index": 1,
                    "title": "国家级智能教育评测平台",
                    "funder": "国家自然科学基金",
                    "grant_no": "62577001",
                    "pi_name": "万振凯",
                    "lab_role": "host",
                    "start_date": "2024-01-01",
                    "end_date": "2027-12-31",
                    "amount_wan": "65.5",
                    "status": "ongoing",
                }
            ],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(content_obj, ensure_ascii=False)}}
                ]
            },
        )

    class StubLLM:
        def __init__(self, client: httpx.AsyncClient) -> None:
            self._client = client
            self.model = "qwen3-stub"

    preview_in: dict[str, Any] = {
        "people": [{"name": "万振凯", "affiliation": "天津天狮学院", "project_role": "applicant"}],
        "achievements": [],
        "projects": [],
        "facilities": [],
        "source_snapshot": {
            "people": [
                {"name": "万振凯", "affiliation": "天津天狮学院", "project_role": "applicant"}
            ],
            "facilities": [],
            "t4": {
                "achievement_raw_items": [
                    scrub_str
                    for scrub_str in [
                        f"万振凯（电话 {TEST_PHONE}）：《跨模态语义对齐评测方法》，软件学报，2025年。"
                    ]
                ],
                "project_raw_items": [
                    f"《国家级智能教育评测平台》（62577001），身份证 {TEST_ID18}，2024年1月立项。"
                ],
            },
        },
    }
    settings = Settings(
        llm_provider="ollama",
        llm_base_url="http://llm.stub.local/v1",
        jwt_secret=STRONG_SECRET_A,
        audit_hmac_key=STRONG_SECRET_C,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_mock_handler)) as ok_http:
        out_preview, out_stats = await reparse_preview_payload_with_llm(
            preview_in,
            as_of_date=date(2026, 3, 1),
            settings=settings,
            llm_obj=StubLLM(ok_http),
        )
    assert len(captured_prompts) == 1
    assert TEST_PHONE not in captured_prompts[0]
    assert TEST_ID18 not in captured_prompts[0]
    assert out_preview["parser_version"] == "v2.1-llm"
    assert out_stats["achievements_parsed"] == 1
    assert out_stats["projects_parsed"] == 1
    assert TEST_PHONE not in json.dumps(out_preview, ensure_ascii=False)
    assert out_preview["projects"][0]["amount_wan"] == "65.50"

    def _fail_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "upstream unavailable"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_fail_handler)
    ) as fail_http:
        with pytest.raises(LLMReparseError) as err:
            await reparse_preview_payload_with_llm(
                preview_in,
                as_of_date=date(2026, 3, 1),
                settings=settings,
                llm_obj=StubLLM(fail_http),
            )
        assert err.value.code == "llm_reparse_failed"


async def test_llm_reparse_null_overrides_db_limits_email_redaction_and_redacted_grants(
    client: AsyncClient,
) -> None:
    _, headers = await _seed_admin_and_login(client)
    outbound_prompts: list[str] = []
    test_work_email = "contact-office@university.edu.cn"

    def _edge_llm_handler(request: httpx.Request) -> httpx.Response:
        req_body = json.loads(request.content.decode())
        outbound_prompts.append(req_body["messages"][-1]["content"])
        content_obj = {
            "achievements": [
                {
                    "index": 1,
                    "kind": "report",
                    "title": "年度智库报告",
                    "first_author": None,
                    "authors_text": None,
                    "venue": None,
                    "year": None,
                    "support_note": None,
                },
                {
                    "index": 2,
                    "kind": "paper",
                    "title": "超长作者边界论文",
                    "first_author": "A" * 100,
                    "authors_text": "Author List",
                    "venue": "Journal",
                    "year": 2025,
                },
            ],
            "projects": [
                {
                    "index": 1,
                    "title": "第一项脱敏编号课题",
                    "funder": "教育部人文社科规划司" + ("甲" * 260),
                    "grant_no": "[已脱敏]",
                    "pi_name": "P" * 100,
                    "lab_role": "host",
                    "start_date": None,
                    "end_date": None,
                    "amount_wan": None,
                    "status": "unknown",
                },
                {
                    "index": 2,
                    "title": "第二项脱敏编号课题",
                    "funder": "天津市科技局",
                    "grant_no": "[已脱敏]",
                    "pi_name": None,
                    "lab_role": "participant",
                    "start_date": "2024-01-01",
                    "end_date": "2026-12-31",
                    "amount_wan": "20",
                    "status": "ongoing",
                },
                {
                    "index": 3,
                    "title": "超长编号正常课题",
                    "funder": "国家社科基金",
                    "grant_no": "G" * 95,
                    "pi_name": None,
                    "lab_role": "host",
                    "start_date": None,
                    "end_date": None,
                    "amount_wan": None,
                    "status": "ongoing",
                },
            ],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps(content_obj, ensure_ascii=False)}}
                ]
            },
        )

    class StubLLM:
        def __init__(self, http_client: httpx.AsyncClient) -> None:
            self._client = http_client
            self.model = "qwen3-stub"

    preview_seed: dict[str, Any] = {
        "people": [{"name": "万振凯", "affiliation": "天津天狮学院", "project_role": "applicant"}],
        "achievements": [],
        "projects": [],
        "facilities": [],
        "source_snapshot": {
            "people": [
                {"name": "万振凯", "affiliation": "天津天狮学院", "project_role": "applicant"}
            ],
            "facilities": [],
            "t4": {
                "achievement_raw_items": [
                    f"课题组编：《年度智库报告》，内部出版，2024年，联系邮箱 {test_work_email}。",
                    "万振凯：《超长作者边界论文》，学报，2025年。",
                ],
                "project_raw_items": [
                    f"《第一项脱敏编号课题》（{TEST_ID18}），万振凯主持，2023-2025，50万元。",
                    f"《第二项脱敏编号课题》（{TEST_ID18}），万振凯主持，2024-2026，20万元。",
                    "《超长编号正常课题》（ABC12345），万振凯主持，2024-2026，30万元。",
                ],
            },
        },
    }
    redact_settings = Settings(
        llm_provider="ollama",
        llm_base_url="http://llm.stub.local/v1",
        llm_redact=True,
        jwt_secret=STRONG_SECRET_A,
        audit_hmac_key=STRONG_SECRET_C,
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_edge_llm_handler)
    ) as edge_http:
        refined_preview, _ = await reparse_preview_payload_with_llm(
            preview_seed,
            as_of_date=date(2026, 3, 1),
            settings=redact_settings,
            llm_obj=StubLLM(edge_http),
        )

    # 1) Outbound LLM redaction honored: work email was redacted to [REDACTED_EMAIL]
    assert len(outbound_prompts) == 1
    assert test_work_email not in outbound_prompts[0]
    assert "[REDACTED_EMAIL]" in outbound_prompts[0]

    # 2) Explicit null corrections from LLM cleared FSM baseline fields
    ach1 = refined_preview["achievements"][0]
    assert ach1["first_author"] is None
    assert ach1["authors_text"] is None
    assert ach1["venue"] is None
    assert ach1["year"] is None

    proj1, proj2, proj3 = refined_preview["projects"]
    assert proj1["start_date"] is None
    assert proj1["end_date"] is None
    assert proj1["amount_wan"] is None
    assert proj1["status"] == "unknown"
    assert proj2["pi_name"] is None

    # 3) DB column caps enforced (<=64 for first_author/grant_no/pi_name, <=255 for funder)
    assert len(refined_preview["achievements"][1]["first_author"]) == 64
    assert len(proj1["pi_name"]) == 64
    assert len(proj1["funder"]) == 255
    assert len(proj3["grant_no"]) == 64

    # 4) Redacted grant numbers rejected & distinct fingerprints preserved across projects
    assert proj1["grant_no"] is None
    assert proj2["grant_no"] is None
    assert "GRANT_NO_REDACTED" in proj1["warnings"]
    assert "GRANT_NO_REDACTED" in proj2["warnings"]
    assert proj1["fingerprint"] != proj2["fingerprint"]

    # 5) End-to-end DB provisioning succeeds without StringDataRightTruncation or duplicate skipping
    docx_bytes = _build_proposal_docx_with_names(applicant_name="万振凯")
    r_up = await client.post(
        "/api/v1/admin/imports",
        headers=headers,
        files={
            "file": (
                "proposal.docx",
                docx_bytes,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r_up.status_code == 200, r_up.text
    job_id = uuid.UUID(r_up.json()["id"])

    from app.models import ImportJob

    maker = get_sessionmaker()
    async with maker() as session:
        job_row = await session.get(ImportJob, job_id)
        assert job_row is not None
        job_row.preview = refined_preview
        await session.commit()

    r_prov = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        headers=headers,
        json={"initial_password": STRONG_NEW_PW},
    )
    assert r_prov.status_code == 200, r_prov.text
    prov_stats = r_prov.json()["stats"]
    assert prov_stats["achievements_inserted"] == 2
    assert prov_stats["projects_inserted"] == 3


async def test_outbound_pii_blocked_with_422_and_signature_audit_and_upload_work(
    client: AsyncClient,
) -> None:
    _, headers = await _seed_admin_and_login(client)
    maker = get_sessionmaker()

    person_id = uuid.uuid4()
    ach_id = uuid.uuid4()
    proj_id = uuid.uuid4()
    async with maker() as session:
        person = LabPerson(
            id=person_id,
            name="李四",
            title="副教授",
            affiliation="天津天狮学院",
            project_role="member",
            bio="教育大数据方向骨干教师",
            entity_fingerprint="a" * 64,
            as_of_time=date(2026, 3, 1),
        )
        ach = LabAchievement(
            id=ach_id,
            kind="paper",
            title="教育评测论文",
            first_author="李四",
            authors_text="李四",
            venue="教育研究",
            year=2025,
            raw_text="李四，《教育评测论文》，教育研究，2025。",
            parse_quality="structured",
            entity_fingerprint="b" * 64,
            as_of_time=date(2026, 3, 1),
        )
        proj = LabProject(
            id=proj_id,
            title="省部级智能阅卷课题",
            funder="天津市教委",
            grant_no="TJ202501",
            pi_name="李四",
            lab_role="host",
            status="ongoing",
            status_source="explicit",
            raw_text="《省部级智能阅卷课题》",
            entity_fingerprint="c" * 64,
            as_of_time=date(2026, 3, 1),
        )
        session.add_all([person, ach, proj])
        await session.commit()

    # 1. Signature upload (exercises session.rollback() before request.stream()) & download audit
    r_up_sig = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=headers,
        files={"file": ("sig.png", PNG_12X12_BYTES, "image/png")},
    )
    assert r_up_sig.status_code == 200, r_up_sig.text
    sig_sha = r_up_sig.json()["signature_sha256"]

    r_dl_sig = await client.get(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=headers,
    )
    assert r_dl_sig.status_code == 200
    r_304 = await client.get(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={**headers, "If-None-Match": f'"{sig_sha}"'},
    )
    assert r_304.status_code == 304

    async with maker() as session:
        sig_reads = list(
            await session.scalars(
                select(AuditEvent).where(
                    AuditEvent.action == "lab.signature.read",
                    AuditEvent.resource_id == str(person_id),
                )
            )
        )
        assert len(sig_reads) == 2

    # 2. Inject unredacted PII directly in DB row to simulate legacy/bypassed dirty state
    async with maker() as session:
        await session.execute(
            text("UPDATE lab_people SET title = :dirty WHERE id = :pid"),
            {"dirty": f"副教授 {TEST_PHONE}", "pid": person_id},
        )
        await session.commit()

    # Every outbound endpoint must block with 422 (never 200 PII leak and never 500 crash)
    r_list = await client.get("/api/v1/lab/people", headers=headers)
    assert r_list.status_code == 422

    r_dossier = await client.get(
        f"/api/v1/lab/people/{person_id}/dossier", headers=headers
    )
    assert r_dossier.status_code == 422
    assert r_dossier.json()["error"]["code"] == "PII_DETECTED"

    r_batch_v22 = await client.post(
        "/api/v1/lab/format/batch",
        headers=headers,
        json={"items": [{"entity": "person", "id": str(person_id)}]},
    )
    assert r_batch_v22.status_code == 422
    assert r_batch_v22.json()["error"]["code"] == "PII_DETECTED"

    r_batch_tab = await client.post(
        "/api/v1/lab/format/batch",
        headers=headers,
        json={"tab": "people"},
    )
    assert r_batch_tab.status_code == 422

    r_export_md = await client.get("/api/v1/lab/export?format=md", headers=headers)
    assert r_export_md.status_code == 422

    r_export_sel = await client.post(
        "/api/v1/lab/export:selected",
        headers=headers,
        json={
            "title": f"汇编 {TEST_PHONE}",
            "items": [{"entity": "achievement", "id": str(ach_id)}],
        },
    )
    # Title PII is scrubbed cleanly when items themselves are clean!
    assert r_export_sel.status_code == 200
    assert TEST_PHONE not in r_export_sel.headers.get("Content-Disposition", "")

    # Signature annex table cell PII is caught by build_selected_docx -> 422 PII_DETECTED
    r_export_annex = await client.post(
        "/api/v1/lab/export:selected",
        headers=headers,
        json={
            "title": "汇编",
            "items": [{"entity": "person", "id": str(person_id)}],
            "options": {"include_signatures_annex": True},
        },
    )
    assert r_export_annex.status_code == 422
    assert r_export_annex.json()["error"]["code"] == "PII_DETECTED"
