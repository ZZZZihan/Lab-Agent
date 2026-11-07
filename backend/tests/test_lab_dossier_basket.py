"""Comprehensive v2.2 tests for Lab Person Dossier, Electronic Signatures, and Proposal Reuse Basket (Spec v2.2 §8.2)."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import re
import struct
import uuid
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import UTC, date, datetime
from pathlib import Path

import docx
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.audit import sanitize_detail
from app.auth import hash_password, issue_token
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.importer.matching import (
    classify_achievement_for_person,
    normalize_person_name,
    split_names,
)
from app.importer.parser import parse_proposal_docx
from app.importer.pii import PIILeakError, assert_no_pii, scrub_free_text
from app.importer.reader import _extract_rel_ids_from_element, extract_embedded_images
from app.importer.signature import validate_signature_image
from app.models import (
    AuditEvent,
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
    User,
)
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

REAL_SAMPLE_DOCX = Path(
    "/home/carryz/.local/share/alma/herdr/uploads/20261106-054942-6cb60859/"
    "教育考试专属大模型构建与安全应用研究_申请书.docx"
)


# ---------------------------------------------------------------------------
# Pure-Python Synthetic Image & .docx Builders
# ---------------------------------------------------------------------------


def make_png_bytes(width: int = 60, height: int = 30, fill_byte: int = 0x80) -> bytes:
    """Create a valid minimal RGB PNG of given dimensions."""
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_chunk = (
        struct.pack(">I", 13)
        + b"IHDR"
        + ihdr_data
        + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF)
    )
    # Use a 1-row compressed payload for huge dimension header tests to keep byte size tiny
    actual_rows = min(height, 16)
    actual_cols = min(width, 16)
    raw_scanlines = b"".join(
        b"\x00" + bytes([fill_byte]) * (actual_cols * 3) for _ in range(actual_rows)
    )
    compressed = zlib.compress(raw_scanlines)
    idat_chunk = (
        struct.pack(">I", len(compressed))
        + b"IDAT"
        + compressed
        + struct.pack(">I", zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF)
    )
    iend_chunk = (
        struct.pack(">I", 0)
        + b"IEND"
        + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    )
    return b"\x89PNG\r\n\x1a\n" + ihdr_chunk + idat_chunk + iend_chunk


def make_jpeg_bytes(width: int = 120, height: int = 50, seed: int = 1) -> bytes:
    """Create a valid minimal baseline JPEG with SOF0, SOS, entropy bytes, and EOI."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0_body = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof0_body) + 2) + sof0_body
    sos_body = b"\x01\x01\x00\x00\x3f\x00"
    sos = b"\xff\xda" + struct.pack(">H", len(sos_body) + 2) + sos_body
    entropy = bytes([(seed + i) % 251 for i in range(24)])
    return b"\xff\xd8" + app0 + sof0 + sos + entropy + b"\xff\xd9"


def make_webp_vp8_bytes(width: int = 80, height: int = 40) -> bytes:
    """Create a valid minimal lossy VP8 WebP image."""
    payload = b"\x10\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width, height) + b"\x00\x00"
    chunk = b"VP8 " + struct.pack("<I", len(payload)) + payload
    riff_size = 4 + len(chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk


def make_webp_vp8l_bytes(width: int = 64, height: int = 32) -> bytes:
    """Create a valid minimal lossless VP8L WebP image."""
    bits = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)
    payload = b"\x2f" + struct.pack("<I", bits) + b"\x00\x00\x00"
    chunk = b"VP8L" + struct.pack("<I", len(payload)) + payload
    riff_size = 4 + len(chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + chunk


def make_webp_vp8x_bytes(width: int = 100, height: int = 50) -> bytes:
    """Create a valid minimal extended VP8X WebP image with an embedded VP8L bitstream chunk."""
    w_minus_1 = (width - 1).to_bytes(3, "little")
    h_minus_1 = (height - 1).to_bytes(3, "little")
    vp8x_payload = b"\x00\x00\x00\x00" + w_minus_1 + h_minus_1
    vp8x_chunk = b"VP8X" + struct.pack("<I", len(vp8x_payload)) + vp8x_payload
    bits = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)
    vp8l_payload = b"\x2f" + struct.pack("<I", bits) + b"\x00\x00\x00"
    vp8l_chunk = b"VP8L" + struct.pack("<I", len(vp8l_payload)) + vp8l_payload
    riff_size = 4 + len(vp8x_chunk) + len(vp8l_chunk)
    return b"RIFF" + struct.pack("<I", riff_size) + b"WEBP" + vp8x_chunk + vp8l_chunk


def build_proposal_docx_with_10_signatures(
    *,
    duplicate_row_sig: bool = False,
    unexpected_cell_sig: bool = False,
    ambiguous_pi_sig: bool = False,
    nearby_pi_diagram: bool = False,
    spaced_sig_header: bool = False,
    blank_member_with_no_pi_sig: bool = False,
    unit_leader_stamp_section: bool = False,
    pi_sig_in_commitment_table: bool = False,
    applicant_in_member_table: bool = False,
    applicant_member_sig_matches_pi: bool = False,
    shared_member_sig_hash: bool = False,
    corrupt_first_member_sig: bool = False,
    inject_external_rel: bool = False,
    wrap_alternate_content: bool = False,
) -> bytes:
    """Build a synthetic 10-person proposal .docx with embedded PI + 9 member signatures and body diagrams."""
    doc = docx.Document()

    # Table 0: Cover table
    t_cover = doc.add_table(rows=4, cols=2)
    t_cover.cell(0, 0).text = "项目名称"
    t_cover.cell(0, 1).text = "教育考试专属大模型构建与安全应用研究"
    t_cover.cell(1, 0).text = "项目申请人"
    t_cover.cell(1, 1).text = "郭建民"
    t_cover.cell(2, 0).text = "所在单位"
    t_cover.cell(2, 1).text = "天津工业大学"
    t_cover.cell(3, 0).text = "填表日期"
    t_cover.cell(3, 1).text = "2026年4月26日"

    # Table 1: Applicant KV rows + Member header row + 9 Member rows
    members = [
        ("万振凯", "教授", "教育理论/创新教学模式", "博士", "天津工业大学", ""),
        ("徐国伟", "教授", "系统架构/平台集成", "博士", "天津工业大学", ""),
        ("杨光", "讲师", "人工智能/智能问答", "硕士", "天津工业大学", ""),
        ("贡丽英", "实验师", "大数据技术/数据挖掘", "硕士", "天津工业大学", ""),
        ("刘岩", "讲师", "教育研究/学术传播", "硕士", "天津工业大学", ""),
        ("王秋慧", "教授", "高等教育规划/教育政策", "博士", "天津工业大学", ""),
        ("王崇帆", "无", "人工智能/自然语言处理", "硕士", "天津工业大学", "2431101262"),
        ("焦良晨", "无", "数据安全/隐私保护", "硕士", "天津工业大学", "2431101226"),
        ("武越", "无", "软件测试/质量管理", "硕士", "天津工业大学", "2431111321"),
    ]
    team_cols = 9 if spaced_sig_header else 8
    t_team = doc.add_table(rows=3 + len(members), cols=team_cols)
    t_team.cell(0, 0).text = "申请人姓名"
    t_team.cell(0, 1).text = "郭建民"
    t_team.cell(0, 2).text = "专业职称"
    t_team.cell(0, 3).text = "正高级实验师"
    t_team.cell(0, 4).text = "工作单位"
    t_team.cell(0, 5).text = "天津工业大学"
    t_team.cell(0, 6).text = "电子信箱"
    t_team.cell(0, 7).text = "guojm@tiangong.edu.cn"

    t_team.cell(1, 0).text = "研究专长"
    t_team.cell(1, 1).text = "智能评测与大模型安全"
    t_team.cell(1, 2).text = "最后学位"
    t_team.cell(1, 3).text = "硕士"

    headers = (
        ["项目组成员", "姓名", "证件号", "专业职务", "研究专长", "学位", "工作单位", "本人签\n字", "备注"]
        if spaced_sig_header
        else ["项目组成员", "姓名", "证件号", "专业职务", "研究专长", "学位", "工作单位", "本人签字"]
    )
    for c_idx, h_text in enumerate(headers):
        t_team.cell(2, c_idx).text = h_text

    for m_idx, (m_name, m_title, m_spec, m_deg, m_aff, m_cert) in enumerate(members):
        r_idx = 3 + m_idx
        t_team.cell(r_idx, 0).text = "项目组成员"
        t_team.cell(r_idx, 1).text = m_name
        t_team.cell(r_idx, 2).text = m_cert
        t_team.cell(r_idx, 3).text = m_title
        t_team.cell(r_idx, 4).text = m_spec
        t_team.cell(r_idx, 5).text = m_deg
        t_team.cell(r_idx, 6).text = m_aff
        if spaced_sig_header:
            t_team.cell(r_idx, 8).text = "无"

        sig_para = t_team.cell(r_idx, 7).paragraphs[0]
        if shared_member_sig_hash and m_idx in (0, 1):
            img_bytes = make_png_bytes(66, 30, fill_byte=0x77)
        elif m_idx == 1 or m_idx == 7:
            img_bytes = make_png_bytes(60 + m_idx, 28, fill_byte=20 + m_idx)
        else:
            img_bytes = make_jpeg_bytes(90 + m_idx, 32, seed=10 + m_idx)
        sig_para.add_run().add_picture(io.BytesIO(img_bytes))

        if duplicate_row_sig and m_idx == 0:
            extra_img = make_png_bytes(50, 25, fill_byte=99)
            sig_para.add_run().add_picture(io.BytesIO(extra_img))

        if unexpected_cell_sig and m_idx == 1:
            other_para = t_team.cell(r_idx, 4).paragraphs[0]
            other_para.add_run().add_picture(
                io.BytesIO(make_png_bytes(40, 20, fill_byte=77))
            )

    if blank_member_with_no_pi_sig:
        blank_row_cells = t_team.add_row().cells
        blank_row_cells[0].text = "项目组成员"
        blank_row_cells[1].text = ""
        blank_row_cells[7].paragraphs[0].add_run().add_picture(
            io.BytesIO(make_png_bytes(70, 30, fill_byte=0x33))
        )

    if applicant_in_member_table:
        app_row_cells = t_team.add_row().cells
        app_row_cells[0].text = "项目组成员"
        app_row_cells[1].text = "郭建民"
        app_row_cells[3].text = "正高级实验师"
        app_row_cells[4].text = "智能评测与大模型安全"
        app_row_cells[5].text = "硕士"
        app_row_cells[6].text = "天津工业大学"
        app_mem_sig = (
            make_jpeg_bytes(165, 82, seed=1)
            if applicant_member_sig_matches_pi
            else make_png_bytes(75, 35, fill_byte=0x5A)
        )
        app_row_cells[7].paragraphs[0].add_run().add_picture(
            io.BytesIO(app_mem_sig)
        )

    # PI commitment block with signature image in preceding paragraph or non-member commitment table
    if pi_sig_in_commitment_table:
        t_pi = doc.add_table(rows=1, cols=2)
        t_pi.cell(0, 0).text = "申请人承诺：上述内容属实"
        pi_cell_p = t_pi.cell(0, 1).paragraphs[0]
        pi_cell_p.text = "申请人（签章）："
        pi_cell_p.add_run().add_picture(
            io.BytesIO(make_jpeg_bytes(165, 82, seed=1))
        )
    else:
        doc.add_paragraph("本人承诺上述填报内容真实有效。")
        pi_sig_p = doc.add_paragraph()
        if not blank_member_with_no_pi_sig:
            pi_sig_p.add_run().add_picture(io.BytesIO(make_jpeg_bytes(165, 82, seed=1)))
        if ambiguous_pi_sig:
            pi_sig_p.add_run().add_picture(io.BytesIO(make_png_bytes(80, 40, fill_byte=55)))
        doc.add_paragraph("申请人（签章）：          2026年 4月 26 日")
    doc.add_paragraph("三、申请人所在单位意见")
    if unit_leader_stamp_section:
        unit_stamp_p = doc.add_paragraph()
        unit_stamp_p.add_run().add_picture(
            io.BytesIO(make_png_bytes(110, 55, fill_byte=0x44))
        )
        doc.add_paragraph("单位负责人（签章）：          2026年 4月 26 日")
        doc.add_paragraph("所在单位负责人（签字）：      2026年 4月 26 日")
    p_sec4 = doc.add_paragraph("四、课题设计论证")
    if nearby_pi_diagram:
        p_sec4.add_run().add_picture(io.BytesIO(make_png_bytes(120, 60, fill_byte=88)))
    doc.add_paragraph("五、完成项目研究的基础和保证")

    # Table 2: Main body & T4 foundation with architecture diagram image (should be ignored by signature rules)
    t_body = doc.add_table(rows=2, cols=1)
    body_para = t_body.cell(0, 0).paragraphs[0]
    body_para.text = "总体架构图如下："
    body_para.add_run().add_picture(io.BytesIO(make_png_bytes(400, 200, fill_byte=120)))
    t_body.cell(1, 0).text = (
        "1.[学术简历]\n郭建民，正高级实验师，长期从事教育测评研究。\n"
        "2.[前期成果]\n"
        "（1）王秋慧(第一):《高等教育高质量发展规划研究》,研究报告,天津市教委,2025年6月。支撑课题研究。\n"
        "（2）徐国伟(第一):《机械类创新人才培养模式探索》,教改论文,《时代教育》,2025年第12期。支撑交叉研究。\n"
        "3.[承担项目]\n"
        "（1）《共享模式下的优质教学资源整合技术研究》,郭建民主持,天津工业大学,2018年4月立项,已通过验收。\n"
        "（2）《天津市高等教育高质量发展规划设计研究》(AGE240005),王秋慧主持,天津市教委,2024年12月立项,在研。\n"
        "4.[与已承担项目的关系]\n本研究在既有课题基础上深化大模型安全测评。"
    )

    # Table 3: Facilities
    t_fac = doc.add_table(rows=2, cols=3)
    t_fac.cell(0, 0).text = "设备名称"
    t_fac.cell(0, 1).text = "型号规格"
    t_fac.cell(0, 2).text = "经费（万元）"
    t_fac.cell(1, 0).text = "高性能AI推理集群"
    t_fac.cell(1, 1).text = "8x GPU Server"
    t_fac.cell(1, 2).text = "180.0"

    buf = io.BytesIO()
    doc.save(buf)
    raw_docx = buf.getvalue()

    if (
        not corrupt_first_member_sig
        and not inject_external_rel
        and not wrap_alternate_content
    ):
        return raw_docx

    # Post-process ZIP entries if testing corrupt media, external relationships, or mc:AlternateContent
    in_zf = zipfile.ZipFile(io.BytesIO(raw_docx), "r")
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as out_zf:
        for info in in_zf.infolist():
            content = in_zf.read(info.filename)
            if corrupt_first_member_sig and info.filename.startswith("word/media/image1."):
                content = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
            elif inject_external_rel and info.filename == "word/_rels/document.xml.rels":
                xml_str = content.decode("utf-8")
                ext_rel = (
                    '<Relationship Id="rIdExternal999" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                    'Target="http://evil.example.com/ leak.png" TargetMode="External"/>'
                )
                xml_str = xml_str.replace("</Relationships>", f"{ext_rel}</Relationships>")
                content = xml_str.encode("utf-8")
            elif wrap_alternate_content and info.filename == "word/document.xml":
                xml_str = content.decode("utf-8")

                def _wrap_drawing(match: re.Match[str]) -> str:
                    drawing_xml = match.group(0)
                    embed_m = re.search(r'r:embed="(rId[^"]+)"', drawing_xml)
                    rid = embed_m.group(1) if embed_m else "rId1"
                    return (
                        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
                        'xmlns:v="urn:schemas-microsoft-com:vml">'
                        f'<mc:Choice Requires="wps">{drawing_xml}</mc:Choice>'
                        f'<mc:Fallback><w:pict><v:shape><v:imagedata r:id="{rid}"/></v:shape></w:pict></mc:Fallback>'
                        "</mc:AlternateContent>"
                    )

                xml_str = re.sub(
                    r"<w:drawing\b.*?</w:drawing>",
                    _wrap_drawing,
                    xml_str,
                    flags=re.DOTALL,
                )
                content = xml_str.encode("utf-8")
            out_zf.writestr(info, content)
    return out_buf.getvalue()


async def _seed_user_and_token(
    *,
    role: str = "admin",
    login_name: str | None = None,
    display_name: str = "管理员",
) -> tuple[User, dict[str, str]]:
    maker = get_sessionmaker()
    uid = uuid.uuid4()
    eff_login = login_name or f"{role}_{uid.hex[:8]}"
    async with maker() as session:
        user = User(
            id=uid,
            email=f"{eff_login}@example.com",
            login_name=eff_login,
            display_name=display_name,
            password_hash=hash_password("ValidPass#2026!"),
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()
    token, _, _ = issue_token(user, get_settings())
    return user, {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Migration & DB Check Constraints (§2 & §8.2 test_migration_v22)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_v22_single_head_and_constraints() -> None:
    alembic_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_cfg)
    heads = script.get_heads()
    assert heads == ["20261110_0009"]

    # Verify downgrade -1 -> upgrade head cycle in subprocess to avoid nested event loop issues
    maker = get_sessionmaker()
    async with maker() as session:
        user = User(
            email="mig_admin@example.com",
            login_name="mig_admin",
            display_name="管理员",
            password_hash=hash_password("ValidPass#2026!"),
            role="admin",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        job = ImportJob(
            uploaded_by=user.id,
            original_filename="mig.docx",
            content_sha256="a" * 64,
            status="provisioned",
            as_of_time=date(2026, 4, 26),
        )
        session.add(job)
        await session.flush()

        # 1) Partial NULLs among the 5 signature columns -> rejected by ck_lab_people_signature_all_or_none
        bad_partial = LabPerson(
            name="测试甲",
            project_role="member",
            entity_fingerprint="1" * 64,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
            signature_data=b"123",
            signature_mime="image/png",
            signature_sha256=None,
            signature_updated_at=datetime.now(UTC),
            signature_source="upload",
        )
        session.add(bad_partial)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    # Re-create base user & job after rollback to test remaining check constraints
    async with maker() as session:
        user = User(
            email="mig_admin2@example.com",
            login_name="mig_admin2",
            display_name="管理员",
            password_hash=hash_password("ValidPass#2026!"),
            role="admin",
            is_active=True,
        )
        session.add(user)
        await session.flush()
        job = ImportJob(
            uploaded_by=user.id,
            original_filename="mig2.docx",
            content_sha256="b" * 64,
            status="provisioned",
            as_of_time=date(2026, 4, 26),
        )
        session.add(job)
        await session.commit()
        job_id = job.id

    # 2) > 2 MiB signature_data -> rejected by ck_lab_people_signature_size
    async with maker() as session:
        oversized = LabPerson(
            name="测试乙",
            project_role="member",
            entity_fingerprint="2" * 64,
            as_of_time=date(2026, 4, 26),
            source_import_id=job_id,
            signature_data=b"x" * 2_097_153,
            signature_mime="image/png",
            signature_sha256="a" * 64,
            signature_updated_at=datetime.now(UTC),
            signature_source="upload",
        )
        session.add(oversized)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    # 3) Invalid mime -> rejected by ck_lab_people_signature_mime
    async with maker() as session:
        bad_mime = LabPerson(
            name="测试丙",
            project_role="member",
            entity_fingerprint="3" * 64,
            as_of_time=date(2026, 4, 26),
            source_import_id=job_id,
            signature_data=b"valid",
            signature_mime="image/svg+xml",
            signature_sha256="a" * 64,
            signature_updated_at=datetime.now(UTC),
            signature_source="upload",
        )
        session.add(bad_mime)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()

    # 4) Uppercase sha256 -> rejected by ck_lab_people_signature_sha256
    async with maker() as session:
        bad_sha = LabPerson(
            name="测试丁",
            project_role="member",
            entity_fingerprint="4" * 64,
            as_of_time=date(2026, 4, 26),
            source_import_id=job_id,
            signature_data=b"valid",
            signature_mime="image/png",
            signature_sha256="A" * 64,
            signature_updated_at=datetime.now(UTC),
            signature_source="upload",
        )
        session.add(bad_sha)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


# ---------------------------------------------------------------------------
# 2. Pure-Python Signature Image Validator (§3.3 & §8.2 test_signature_validator)
# ---------------------------------------------------------------------------


def test_signature_validator_formats_and_bounds() -> None:
    # Valid PNG, JPEG, WebP (VP8, VP8L, VP8X)
    png_bytes = make_png_bytes(86, 54)
    c_png = validate_signature_image(png_bytes)
    assert c_png.ok is True
    assert c_png.error_code is None
    assert c_png.mime == "image/png"
    assert (c_png.width, c_png.height) == (86, 54)
    assert c_png.sha256 == hashlib.sha256(png_bytes).hexdigest()

    jpg_bytes = make_jpeg_bytes(165, 82)
    c_jpg = validate_signature_image(jpg_bytes)
    assert c_jpg.ok is True
    assert c_jpg.mime == "image/jpeg"
    assert (c_jpg.width, c_jpg.height) == (165, 82)
    assert c_jpg.sha256 == hashlib.sha256(jpg_bytes).hexdigest()

    for webp_b, expected_dims in (
        (make_webp_vp8_bytes(80, 40), (80, 40)),
        (make_webp_vp8l_bytes(64, 32), (64, 32)),
        (make_webp_vp8x_bytes(100, 50), (100, 50)),
    ):
        c_webp = validate_signature_image(webp_b)
        assert c_webp.ok is True
        assert c_webp.mime == "image/webp"
        assert (c_webp.width, c_webp.height) == expected_dims

    # Size bounds
    assert validate_signature_image(b"").error_code == "SIGNATURE_TOO_LARGE"
    assert (
        validate_signature_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 2_097_150).error_code
        == "SIGNATURE_TOO_LARGE"
    )

    # Rejected formats (SVG, HTML, PDF, BMP, GIF, random)
    for bad_fmt in (
        b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
        b"<!DOCTYPE html><html><script>alert(1)</script></html>",
        b"%PDF-1.7\n...",
        b"BM\x36\x00\x00\x00",
        b"GIF89a\x10\x00\x10\x00",
        b"random_garbage_bytes_1234567890",
    ):
        chk = validate_signature_image(bad_fmt)
        assert chk.ok is False
        assert chk.error_code == "SIGNATURE_REJECTED_FORMAT"
        assert chk.sha256 is None

    # Corrupt / truncated PNG (including IHDR+IEND without IDAT), JPEG (including truncated after SOS without EOI), WebP
    assert validate_signature_image(png_bytes[:-8]).error_code == "SIGNATURE_CORRUPT"
    ihdr_iend_only_png = png_bytes[:33] + png_bytes[-12:]
    assert (
        validate_signature_image(ihdr_iend_only_png).error_code == "SIGNATURE_CORRUPT"
    )
    assert validate_signature_image(jpg_bytes[:15]).error_code == "SIGNATURE_CORRUPT"
    assert validate_signature_image(jpg_bytes[:-2]).error_code == "SIGNATURE_CORRUPT"
    bad_webp = b"RIFF" + struct.pack("<I", 9999) + b"WEBP" + b"VP8 " + struct.pack("<I", 10) + b"0" * 10
    vp8x_header_only_webp = (
        b"RIFF"
        + struct.pack("<I", 22)
        + b"WEBP"
        + b"VP8X"
        + struct.pack("<I", 10)
        + b"\x00\x00\x00\x00"
        + (59).to_bytes(3, "little")
        + (29).to_bytes(3, "little")
    )
    assert (
        validate_signature_image(vp8x_header_only_webp).error_code
        == "SIGNATURE_CORRUPT"
    )

    # PII redaction beside ASCII contact labels while preserving 64-char SHA-256 hex hashes
    synth_mob = "138" + "12345678"
    scrubbed_txt, scrub_cnt = scrub_free_text(
        f"联系电话Tel{synth_mob}，座机Tel022-83951234，签名哈希{c_jpg.sha256}"
    )
    assert scrub_cnt == 2
    assert scrubbed_txt == f"联系电话Tel[已脱敏]，座机Tel[已脱敏]，签名哈希{c_jpg.sha256}"
    with pytest.raises(PIILeakError):
        assert_no_pii(f"联系电话Tel{synth_mob}")
    assert_no_pii(scrubbed_txt)
    phone_like_sha = "16816937983d6576a9b6c96878ac3ffcba4f2af7bf9a327cd5ab5b30e1273a34"
    sanitized_audit = sanitize_detail(
        {
            "person_id": str(uuid.uuid4()),
            "sha256": phone_like_sha,
            "hashes": [phone_like_sha],
            "channel": "single",
        }
    )
    assert sanitized_audit["sha256"] == phone_like_sha
    assert validate_signature_image(bad_webp).error_code == "SIGNATURE_CORRUPT"

    # Bad dimensions (< 8x8, > 6000, > 20M pixels)
    assert validate_signature_image(make_png_bytes(4, 10)).error_code == "SIGNATURE_BAD_DIMENSIONS"
    assert validate_signature_image(make_png_bytes(6001, 20)).error_code == "SIGNATURE_BAD_DIMENSIONS"
    assert validate_signature_image(make_png_bytes(5000, 5000)).error_code == "SIGNATURE_BAD_DIMENSIONS"


# ---------------------------------------------------------------------------
# 3. .docx Electronic Signature Extraction (§3.4 & §8.2 test_docx_signature_extract)
# ---------------------------------------------------------------------------


def test_docx_signature_extract_synthetic_and_edge_cases() -> None:
    # Normal 10-person proposal with body diagram in Table 2 and external rel ignored
    raw_docx = build_proposal_docx_with_10_signatures(inject_external_rel=True)
    imgs = extract_embedded_images(raw_docx)
    assert all(img.media_path.startswith("word/media/") for img in imgs)
    assert all(img.rel_id != "rIdExternal999" for img in imgs)

    parsed = parse_proposal_docx("proposal.docx", raw_docx)
    preview = parsed["preview"]
    sigs = preview["signatures"]
    assert isinstance(sigs, list)
    assert len(sigs) == 10
    assert sigs[0]["person_temp_id"] == "p1"
    assert sigs[0]["person_name"] == "郭建民"
    assert sigs[0]["role"] == "pi"
    assert sigs[0]["source_kind"] == "pi_commitment_paragraph"
    for idx in range(1, 10):
        assert sigs[idx]["person_temp_id"] == f"p{idx + 1}"
        assert sigs[idx]["role"] == "member"
        assert sigs[idx]["source_kind"] == "member_table_cell"
    sig_warns = [
        w for w in preview["warnings"] if str(w.get("code", "")).startswith("SIGNATURE_")
    ]
    assert sig_warns == []

    # Edge case 1: 2 images in a member signature cell -> SIGNATURE_MULTIPLE_IN_ROW
    dup_docx = build_proposal_docx_with_10_signatures(duplicate_row_sig=True)
    dup_parsed = parse_proposal_docx("dup.docx", dup_docx)
    dup_codes = [w["code"] for w in dup_parsed["preview"]["warnings"]]
    assert "SIGNATURE_MULTIPLE_IN_ROW" in dup_codes
    assert len(dup_parsed["preview"]["signatures"]) == 9

    # Edge case 2: image in a non-signature cell of a member row -> SIGNATURE_UNEXPECTED_CELL
    unexp_docx = build_proposal_docx_with_10_signatures(unexpected_cell_sig=True)
    unexp_parsed = parse_proposal_docx("unexp.docx", unexp_docx)
    unexp_codes = [w["code"] for w in unexp_parsed["preview"]["warnings"]]
    assert "SIGNATURE_UNEXPECTED_CELL" in unexp_codes
    assert len(unexp_parsed["preview"]["signatures"]) == 10

    # Edge case 3: 2 images in PI commitment window at same distance -> SIGNATURE_AMBIGUOUS_PI
    ambig_docx = build_proposal_docx_with_10_signatures(ambiguous_pi_sig=True)
    ambig_parsed = parse_proposal_docx("ambig.docx", ambig_docx)
    ambig_codes = [w["code"] for w in ambig_parsed["preview"]["warnings"]]
    assert "SIGNATURE_AMBIGUOUS_PI" in ambig_codes
    assert len(ambig_parsed["preview"]["signatures"]) == 9

    # Edge case 3b: PI signature at distance -1 vs unrelated diagram at distance +2 -> closest wins without ambiguity
    nearby_docx = build_proposal_docx_with_10_signatures(nearby_pi_diagram=True)
    nearby_parsed = parse_proposal_docx("nearby.docx", nearby_docx)
    nearby_codes = [w["code"] for w in nearby_parsed["preview"]["warnings"]]
    assert "SIGNATURE_AMBIGUOUS_PI" not in nearby_codes
    assert len(nearby_parsed["preview"]["signatures"]) == 10
    assert nearby_parsed["preview"]["signatures"][0]["person_temp_id"] == "p1"

    # Edge case 3c: Spaced/line-broken signature header ("本人签\n字") followed by a "备注" column
    spaced_hdr_docx = build_proposal_docx_with_10_signatures(spaced_sig_header=True)
    spaced_hdr_parsed = parse_proposal_docx("spaced_hdr.docx", spaced_hdr_docx)
    spaced_hdr_codes = [w["code"] for w in spaced_hdr_parsed["preview"]["warnings"]]
    assert "SIGNATURE_UNEXPECTED_CELL" not in spaced_hdr_codes
    assert len(spaced_hdr_parsed["preview"]["signatures"]) == 10

    # Edge case 3d: Blank-name member row with signature + no PI signature -> orphan member signature never bound to PI
    blank_mem_docx = build_proposal_docx_with_10_signatures(
        blank_member_with_no_pi_sig=True
    )
    blank_mem_parsed = parse_proposal_docx("blank_mem.docx", blank_mem_docx)
    assert all(
        s["person_temp_id"] != "p1" for s in blank_mem_parsed["preview"]["signatures"]
    )
    assert len(blank_mem_parsed["preview"]["signatures"]) == 9

    # Edge case 3e: Unit leader signature section ("单位负责人（签章）：") after PI signature does not match PI anchor or trigger SIGNATURE_AMBIGUOUS_PI
    unit_ldr_docx = build_proposal_docx_with_10_signatures(
        unit_leader_stamp_section=True
    )
    unit_ldr_parsed = parse_proposal_docx("unit_ldr.docx", unit_ldr_docx)
    unit_ldr_codes = [w["code"] for w in unit_ldr_parsed["preview"]["warnings"]]
    assert "SIGNATURE_AMBIGUOUS_PI" not in unit_ldr_codes
    assert len(unit_ldr_parsed["preview"]["signatures"]) == 10
    assert unit_ldr_parsed["preview"]["signatures"][0]["person_temp_id"] == "p1"
    assert (
        unit_ldr_parsed["preview"]["signatures"][0]["sha256"]
        == sigs[0]["sha256"]
    )

    # Edge case 3f: PI signature in a non-member commitment table where cell 0 has commitment text and cell 1 has "申请人（签章）：" + image
    pi_tbl_docx = build_proposal_docx_with_10_signatures(
        pi_sig_in_commitment_table=True
    )
    pi_tbl_parsed = parse_proposal_docx("pi_tbl.docx", pi_tbl_docx)
    pi_tbl_sig_warns = [
        w
        for w in pi_tbl_parsed["preview"]["warnings"]
        if str(w.get("code", "")).startswith("SIGNATURE_")
    ]
    assert pi_tbl_sig_warns == []
    assert len(pi_tbl_parsed["preview"]["signatures"]) == 10
    assert pi_tbl_parsed["preview"]["signatures"][0]["person_temp_id"] == "p1"
    assert (
        pi_tbl_parsed["preview"]["signatures"][0]["sha256"]
        == sigs[0]["sha256"]
    )

    # Edge case 3g: mc:AlternateContent with DrawingML in mc:Choice and VML in mc:Fallback selects only one branch
    alt_elem = ET.fromstring(
        '<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<mc:AlternateContent>"
        '<mc:Choice Requires="wps"><w:drawing><a:blip r:embed="rId1"/></w:drawing></mc:Choice>'
        '<mc:Fallback><w:pict><v:shape><v:imagedata r:id="rId1"/></v:shape></w:pict></mc:Fallback>'
        "</mc:AlternateContent>"
        "</w:r>"
    )
    assert _extract_rel_ids_from_element(alt_elem) == ["rId1"]

    alt_docx = build_proposal_docx_with_10_signatures(wrap_alternate_content=True)
    assert len(extract_embedded_images(alt_docx)) == len(imgs)
    alt_parsed = parse_proposal_docx("alt.docx", alt_docx)
    alt_sig_warns = [
        w
        for w in alt_parsed["preview"]["warnings"]
        if str(w.get("code", "")).startswith("SIGNATURE_")
    ]
    assert alt_sig_warns == []
    assert len(alt_parsed["preview"]["signatures"]) == 10

    # Edge case 3h: Applicant appears in member table with different vs identical signature image vs PI commitment section
    conflict_docx = build_proposal_docx_with_10_signatures(
        applicant_in_member_table=True,
        applicant_member_sig_matches_pi=False,
    )
    conflict_parsed = parse_proposal_docx("pi_conflict.docx", conflict_docx)
    conflict_sigs = conflict_parsed["preview"]["signatures"]
    assert len(conflict_sigs) == 10
    expected_member_pi_sha = hashlib.sha256(
        make_png_bytes(75, 35, fill_byte=0x5A)
    ).hexdigest()
    expected_commit_pi_sha = hashlib.sha256(
        make_jpeg_bytes(165, 82, seed=1)
    ).hexdigest()
    assert conflict_sigs[0]["person_temp_id"] == "p1"
    assert conflict_sigs[0]["source_kind"] == "member_table_cell"
    assert conflict_sigs[0]["sha256"] == expected_member_pi_sha
    conflict_warns = [
        w
        for w in conflict_parsed["preview"]["warnings"]
        if w.get("code") == "SIG_PI_CONFLICT"
    ]
    assert len(conflict_warns) == 1
    assert conflict_warns[0]["person_temp_id"] == "p1"
    assert conflict_warns[0]["person_name"] == "郭建民"
    assert conflict_warns[0]["member_sha256"] == expected_member_pi_sha
    assert conflict_warns[0]["pi_sha256"] == expected_commit_pi_sha

    same_pi_docx = build_proposal_docx_with_10_signatures(
        applicant_in_member_table=True,
        applicant_member_sig_matches_pi=True,
    )
    same_pi_parsed = parse_proposal_docx("pi_same.docx", same_pi_docx)
    same_pi_sigs = same_pi_parsed["preview"]["signatures"]
    assert len(same_pi_sigs) == 10
    assert same_pi_sigs[0]["person_temp_id"] == "p1"
    assert same_pi_sigs[0]["sha256"] == expected_commit_pi_sha
    assert all(
        w.get("code") != "SIG_PI_CONFLICT"
        for w in same_pi_parsed["preview"]["warnings"]
    )

    # Edge case 3i: Two distinct members share the exact same signature image (same sha256) -> retain bindings and emit SIG_DUPLICATE_HASH
    shared_hash_docx = build_proposal_docx_with_10_signatures(
        shared_member_sig_hash=True
    )
    shared_hash_parsed = parse_proposal_docx("shared_hash.docx", shared_hash_docx)
    shared_hash_sigs = shared_hash_parsed["preview"]["signatures"]
    assert len(shared_hash_sigs) == 10
    expected_shared_sha = hashlib.sha256(
        make_png_bytes(66, 30, fill_byte=0x77)
    ).hexdigest()
    assert shared_hash_sigs[1]["person_temp_id"] == "p2"
    assert shared_hash_sigs[1]["sha256"] == expected_shared_sha
    assert shared_hash_sigs[2]["person_temp_id"] == "p3"
    assert shared_hash_sigs[2]["sha256"] == expected_shared_sha
    dup_hash_warns = [
        w
        for w in shared_hash_parsed["preview"]["warnings"]
        if w.get("code") == "SIG_DUPLICATE_HASH"
    ]
    assert len(dup_hash_warns) == 1
    assert dup_hash_warns[0]["sha256"] == expected_shared_sha
    assert dup_hash_warns[0]["person_temp_ids"] == ["p2", "p3"]
    assert dup_hash_warns[0]["person_names"] == ["万振凯", "徐国伟"]

    # Edge case 4: corrupt / SVG image inside member signature cell -> warning, does not abort parse
    corrupt_docx = build_proposal_docx_with_10_signatures(corrupt_first_member_sig=True)
    corrupt_parsed = parse_proposal_docx("corrupt.docx", corrupt_docx)
    corrupt_codes = [w["code"] for w in corrupt_parsed["preview"]["warnings"]]
    assert "SIGNATURE_REJECTED_FORMAT" in corrupt_codes
    assert len(corrupt_parsed["preview"]["signatures"]) == 9

    # Empirical check against real sample .docx if present on this machine
    if REAL_SAMPLE_DOCX.is_file():
        real_parsed = parse_proposal_docx(REAL_SAMPLE_DOCX.name, REAL_SAMPLE_DOCX.read_bytes())
        real_sigs = real_parsed["preview"]["signatures"]
        assert len(real_sigs) == 10
        expected_shas = [
            ("p1", "郭建民", "b296e294cd7a3f2a87cce207d83e7f3cd8efabe59362030922ce218e44703c0e"),
            ("p2", "万振凯", "54c89f4b83bede328e7f59dd2aa92504cc7ce94aef2bb67b0487008029a557de"),
            ("p3", "徐国伟", "548b1242cac22775fdfe7e3eaf23b32afb50453b7c2fbe1fd7aeebb8e86317a5"),
            ("p4", "杨光", "948cccac0129f12f28759fbe9269d758aa4f9e0c181550bb3fa4af4a1ac38780"),
            ("p5", "贡丽英", "ce8c62e2f2362c6b5c9bf1fcd9e11fccdcda2da697f8e16cefb471b00d5c53f1"),
            ("p6", "刘岩", "97733338da4ad391d1090cda345f1fe7ae3461780ec18d03886249f8ff381d8d"),
            ("p7", "王秋慧", "3626980270fe9ecf1501a61c72bc42ebc0f206a0246cc6a0bc6707dec8f59e4d"),
            ("p8", "王崇帆", "394db8a6cccafd8cf0280d91a28c242028e787596d22115b6149b9648ce0ec3e"),
            ("p9", "焦良晨", "bdb5e9950a3382f3903405a91762e6798d2d7fda4235857bb94a2e6b9772ce3e"),
            ("p10", "武越", "0b8d46e331a3d24f12f9c116def4d9a1e402f9cfe2ec79b73a5b37fef34d8f99"),
        ]
        for s_item, (exp_id, exp_name, exp_sha) in zip(real_sigs, expected_shas, strict=True):
            assert s_item["person_temp_id"] == exp_id
            assert s_item["person_name"] == exp_name
            assert s_item["sha256"] == exp_sha


# ---------------------------------------------------------------------------
# 4. Import Confirm Signature Persistence (§3.4.6 & §8.2 test_import_confirm_signatures)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_confirm_signatures_lifecycle(client: AsyncClient) -> None:
    _, admin_headers = await _seed_user_and_token(role="admin")
    raw_docx = build_proposal_docx_with_10_signatures()

    # 1) Upload & parse: preview response must NOT contain _signature_blobs or >512 char base64
    r_up = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert r_up.status_code == 200
    up_json = r_up.json()
    job_id = up_json["job_id"]
    assert "_signature_blobs" not in up_json["preview"]
    assert len(up_json["preview"]["signatures"]) == 10
    raw_resp_text = r_up.text
    assert "_signature_blobs" not in raw_resp_text

    # Confirm 1st time -> saved == 10, _signature_blobs cleared from DB session
    r_conf1 = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        headers=admin_headers,
        json={},
    )
    assert r_conf1.status_code == 200
    sig_res1 = r_conf1.json()["signature_result"]
    assert sig_res1 == {
        "saved": 10,
        "unchanged": 0,
        "kept_upload": 0,
        "skipped": 0,
        "rejected": 0,
        "person_not_imported": 0,
    }

    maker = get_sessionmaker()
    async with maker() as session:
        db_job = await session.get(ImportJob, uuid.UUID(job_id))
        assert db_job is not None
        assert "_signature_blobs" not in (db_job.preview or {})
        people_rows = list(await session.scalars(select(LabPerson)))
        assert len(people_rows) == 10
        for lp in people_rows:
            assert lp.signature_mime in ("image/png", "image/jpeg")
            assert lp.signature_sha256 is not None and len(lp.signature_sha256) == 64
            assert lp.signature_updated_at is not None
            assert lp.signature_source == "docx_import"
        first_updated_map = {lp.id: lp.signature_updated_at for lp in people_rows}
        wan_person = next(lp for lp in people_rows if lp.name == "万振凯")
        wan_id = str(wan_person.id)

    # 2) Re-upload same .docx and confirm -> unchanged == 10, signature_updated_at unchanged
    r_up2 = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_reimport.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job2_id = r_up2.json()["job_id"]
    r_conf2 = await client.post(
        f"/api/v1/admin/imports/{job2_id}/provision",
        headers=admin_headers,
        json={},
    )
    assert r_conf2.status_code == 200
    assert r_conf2.json()["signature_result"]["unchanged"] == 10
    async with maker() as session:
        for lp in await session.scalars(select(LabPerson)):
            assert lp.signature_updated_at == first_updated_map[lp.id]

    # 3) Manual PUT /signature on 万振凯 (source='upload'), then re-import with skip_signature_ids,
    #    excluded person, and tampered blob
    manual_png = make_png_bytes(100, 45, fill_byte=0x33)
    r_put = await client.put(
        f"/api/v1/lab/people/{wan_id}/signature",
        headers=admin_headers,
        files={"file": ("manual.png", manual_png, "image/png")},
    )
    assert r_put.status_code == 200
    assert r_put.json()["signature_source"] == "upload"
    manual_sha = r_put.json()["signature_sha256"]

    r_up3 = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_v3.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job3_id = r_up3.json()["job_id"]
    sigs3 = r_up3.json()["preview"]["signatures"]
    p4_sha = next(s["sha256"] for s in sigs3 if s["person_temp_id"] == "p4")

    # Tamper with p4's blob in DB so p4 triggers `rejected`
    async with maker() as session:
        db_job3 = await session.get(ImportJob, uuid.UUID(job3_id))
        assert db_job3 is not None
        prev3 = dict(db_job3.preview or {})
        blobs3 = dict(prev3.get("_signature_blobs") or {})
        blobs3[p4_sha] = "not_valid_base64_!!!"
        prev3["_signature_blobs"] = blobs3
        db_job3.preview = prev3
        await session.commit()

    # Confirm job3 with:
    # - p2 (万振凯) has manual upload -> kept_upload = 1
    # - p3 in skip_signature_ids -> skipped = 1
    # - p4 has tampered blob -> rejected = 1
    # - p5 excluded from people -> person_not_imported = 1
    # - remaining 6 people -> unchanged = 6
    r_conf3 = await client.post(
        f"/api/v1/admin/imports/{job3_id}/provision",
        headers=admin_headers,
        json={
            "skip_signature_ids": ["p3"],
            "people": [{"key": "p5", "exclude": True}],
        },
    )
    assert r_conf3.status_code == 200
    assert "_pre_merge_signatures" not in r_conf3.json()["stats"]
    assert r_conf3.json()["signature_result"] == {
        "saved": 0,
        "unchanged": 6,
        "kept_upload": 1,
        "skipped": 1,
        "rejected": 1,
        "person_not_imported": 1,
    }

    # Verify 万振凯's manual signature was never overwritten
    async with maker() as session:
        wan_after = await session.get(LabPerson, uuid.UUID(wan_id))
        assert wan_after is not None
        assert wan_after.signature_source == "upload"
        assert wan_after.signature_sha256 == manual_sha
        guo_before_merge = next(
            lp for lp in await session.scalars(select(LabPerson)) if lp.name == "郭建民"
        )
        guo_id = str(guo_before_merge.id)
        guo_orig_sha = guo_before_merge.signature_sha256
        # Clear 郭建民's signature before testing merge + revert restoration
        guo_before_merge.signature_data = None
        guo_before_merge.signature_mime = None
        guo_before_merge.signature_sha256 = None
        guo_before_merge.signature_updated_at = None
        guo_before_merge.signature_source = None
        await session.commit()

    # 4) Merge a new job onto existing people -> 郭建民 gets docx_import signature; then revert -> 郭建民's signature is cleared back to None while 万振凯's manual upload stays intact
    r_up4 = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_merge_revert.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job4_id = r_up4.json()["job_id"]
    r_merge4 = await client.post(
        f"/api/v1/admin/imports/{job4_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge4.status_code == 200, r_merge4.text
    async with maker() as session:
        guo_merged = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_merged is not None
        assert guo_merged.signature_sha256 == guo_orig_sha
        assert guo_merged.signature_source == "docx_import"

    r_rev4 = await client.post(
        f"/api/v1/admin/imports/{job4_id}/revert",
        headers=admin_headers,
    )
    assert r_rev4.status_code == 200, r_rev4.text
    async with maker() as session:
        guo_reverted = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_reverted is not None
        assert guo_reverted.signature_sha256 is None
        assert guo_reverted.signature_source is None
        wan_still_upload = await session.get(LabPerson, uuid.UUID(wan_id))
        assert wan_still_upload is not None
        assert wan_still_upload.signature_source == "upload"
        assert wan_still_upload.signature_sha256 == manual_sha

    # 4b) Re-merge the same reverted job4 -> signature blobs are preserved and reapplied (no missing_blob rejection)
    r_remerge4 = await client.post(
        f"/api/v1/admin/imports/{job4_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_remerge4.status_code == 200, r_remerge4.text
    remerge4_sig = r_remerge4.json()["signature_result"]
    assert remerge4_sig["saved"] >= 1
    assert remerge4_sig["rejected"] == 0
    async with maker() as session:
        guo_remerged = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_remerged is not None
        assert guo_remerged.signature_sha256 == guo_orig_sha
        assert guo_remerged.signature_source == "docx_import"

    # Revert job4 again so step 5 starts with guo having no signature
    r_rev4_again = await client.post(
        f"/api/v1/admin/imports/{job4_id}/revert",
        headers=admin_headers,
    )
    assert r_rev4_again.status_code == 200, r_rev4_again.text

    # 5) Out-of-order revert: merge job A (None -> S1) then merge job B (S1 -> S2);
    #    reverting job A first rebases job B's snapshot to None, so reverting job B restores None (not S1)
    r_up_a = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_chain_a.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job_a_id = r_up_a.json()["job_id"]
    r_merge_a = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_a.status_code == 200, r_merge_a.text

    r_up_b = await client.post(
        "/api/v1/admin/imports/proposal",
        headers=admin_headers,
        files={
            "file": (
                "proposal_chain_b.docx",
                raw_docx,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    job_b_id = r_up_b.json()["job_id"]
    # Replace p1 (郭建民) signature in job_b with a distinct PNG (S2)
    s2_bytes = make_png_bytes(120, 50, fill_byte=0x77)
    s2_sha = hashlib.sha256(s2_bytes).hexdigest()
    async with maker() as session:
        db_job_b = await session.get(ImportJob, uuid.UUID(job_b_id))
        assert db_job_b is not None
        prev_b = dict(db_job_b.preview or {})
        sigs_b = [dict(s) for s in (prev_b.get("signatures") or [])]
        for s_item in sigs_b:
            if s_item.get("person_temp_id") == "p1":
                s_item["sha256"] = s2_sha
                s_item["mime"] = "image/png"
        prev_b["signatures"] = sigs_b
        blobs_b = dict(prev_b.get("_signature_blobs") or {})
        blobs_b[s2_sha] = base64.b64encode(s2_bytes).decode("ascii")
        prev_b["_signature_blobs"] = blobs_b
        db_job_b.preview = prev_b
        await session.commit()

    r_merge_b = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_b.status_code == 200, r_merge_b.text
    async with maker() as session:
        guo_after_b = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_b is not None
        assert guo_after_b.signature_sha256 == s2_sha

    # Re-merge job A with p1 in skip_signature_ids -> must not claim B's active S2
    r_remerge_a_skip = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": [], "skip_signature_ids": ["p1"]},
    )
    assert r_remerge_a_skip.status_code == 200, r_remerge_a_skip.text

    # Revert job A first (while S2 is active) -> S2 stays active, and B's snapshot is rebased to None (S0)
    r_rev_a = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/revert", headers=admin_headers
    )
    assert r_rev_a.status_code == 200, r_rev_a.text
    async with maker() as session:
        guo_after_rev_a = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_rev_a is not None
        assert guo_after_rev_a.signature_sha256 == s2_sha
        assert guo_after_rev_a.signature_source == "docx_import"

    # Then revert job B -> must restore None (S0), not S1
    r_rev_b = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/revert", headers=admin_headers
    )
    assert r_rev_b.status_code == 200, r_rev_b.text
    async with maker() as session:
        guo_after_both_reverts = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_both_reverts is not None
        assert guo_after_both_reverts.signature_sha256 is None
        assert guo_after_both_reverts.signature_source is None

    # 5b) Re-merge earlier import A without skipping signature after B replaced S1 with S2:
    #     A writes S1 again and rebases B's pre-merge snapshot to None while updating A's pre-merge snapshot to S2.
    #     Reverting A restores B's active S2, and reverting B then restores None.
    r_merge_a2 = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_a2.status_code == 200, r_merge_a2.text
    r_merge_b2 = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_merge_b2.status_code == 200, r_merge_b2.text
    r_remerge_a_write = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/merge",
        headers=admin_headers,
        json={"decisions": []},
    )
    assert r_remerge_a_write.status_code == 200, r_remerge_a_write.text
    async with maker() as session:
        guo_after_remerge_a = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_remerge_a is not None
        assert guo_after_remerge_a.signature_sha256 == guo_orig_sha

    r_rev_a2 = await client.post(
        f"/api/v1/admin/imports/{job_a_id}/revert", headers=admin_headers
    )
    assert r_rev_a2.status_code == 200, r_rev_a2.text
    async with maker() as session:
        guo_after_rev_a2 = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_rev_a2 is not None
        assert guo_after_rev_a2.signature_sha256 == s2_sha
        assert guo_after_rev_a2.signature_source == "docx_import"

    r_rev_b2 = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/revert", headers=admin_headers
    )
    assert r_rev_b2.status_code == 200, r_rev_b2.text
    async with maker() as session:
        guo_after_rev_b2 = await session.get(LabPerson, uuid.UUID(guo_id))
        assert guo_after_rev_b2 is not None
        assert guo_after_rev_b2.signature_sha256 is None
        assert guo_after_rev_b2.signature_source is None

    # 6) Conflicting person in exclude_keys does not trigger 422 merge_conflict_unresolved
    async with maker() as session:
        db_job_b2 = await session.get(ImportJob, uuid.UUID(job_b_id))
        assert db_job_b2 is not None
        prev_b2 = dict(db_job_b2.preview or {})
        ppl_b2 = [dict(p) for p in (prev_b2.get("people") or [])]
        for p_item in ppl_b2:
            if p_item.get("name") == "万振凯":
                p_item["title"] = "特聘冲突教授"
        prev_b2["people"] = ppl_b2
        db_job_b2.preview = prev_b2
        await session.commit()

    r_merge_excl = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": [], "exclude_keys": ["p2"]},
    )
    assert r_merge_excl.status_code == 200, r_merge_excl.text
    async with maker() as session:
        wan_after_excl = await session.get(LabPerson, uuid.UUID(wan_id))
        assert wan_after_excl is not None
        assert wan_after_excl.title == "教授"

    # 7) Newly merged unlinked person who later receives a manual signature upload is preserved on revert
    async with maker() as session:
        db_job_b3 = await session.get(ImportJob, uuid.UUID(job_b_id))
        assert db_job_b3 is not None
        prev_b3 = dict(db_job_b3.preview or {})
        ppl_b3 = [dict(p) for p in (prev_b3.get("people") or [])]
        ppl_b3.append(
            {
                "key": "p_new_unlinked",
                "temp_id": "p_new_unlinked",
                "name": "赵六",
                "title": "助理研究员",
                "affiliation": "天津工业大学",
                "project_role": "member",
                "tier": "T2",
            }
        )
        prev_b3["people"] = ppl_b3
        db_job_b3.preview = prev_b3
        await session.commit()

    r_merge_new = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/merge",
        headers=admin_headers,
        json={"decisions": [], "exclude_keys": ["p2"]},
    )
    assert r_merge_new.status_code == 200, r_merge_new.text
    async with maker() as session:
        zhao_p = await session.scalar(select(LabPerson).where(LabPerson.name == "赵六"))
        assert zhao_p is not None
        assert zhao_p.user_id is None
        zhao_id = str(zhao_p.id)

    zhao_manual_png = make_png_bytes(88, 38, fill_byte=0x66)
    zhao_manual_sha = hashlib.sha256(zhao_manual_png).hexdigest()
    r_put_zhao = await client.put(
        f"/api/v1/lab/people/{zhao_id}/signature",
        headers=admin_headers,
        files={"file": ("zhao.png", zhao_manual_png, "image/png")},
    )
    assert r_put_zhao.status_code == 200, r_put_zhao.text

    r_rev_new = await client.post(
        f"/api/v1/admin/imports/{job_b_id}/revert",
        headers=admin_headers,
    )
    assert r_rev_new.status_code == 200, r_rev_new.text
    async with maker() as session:
        zhao_after_rev = await session.get(LabPerson, uuid.UUID(zhao_id))
        assert zhao_after_rev is not None
        assert zhao_after_rev.signature_source == "upload"
        assert zhao_after_rev.signature_sha256 == zhao_manual_sha


# ---------------------------------------------------------------------------
# 5. Signature CRUD, ETag/If-Match, Security Headers & Deferred Loading (§5 & §8.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signature_crud_headers_concurrency_and_deferred_sql(
    client: AsyncClient,
) -> None:
    _, admin_headers = await _seed_user_and_token(role="admin", login_name="admin_sig")
    self_user, self_headers = await _seed_user_and_token(
        role="member", login_name="wan_self", display_name="万振凯"
    )
    _, other_member_headers = await _seed_user_and_token(
        role="member", login_name="other_mem", display_name="徐国伟"
    )
    _, viewer_headers = await _seed_user_and_token(
        role="viewer", login_name="viewer_u", display_name="访客"
    )

    maker = get_sessionmaker()
    async with maker() as session:
        job = ImportJob(
            uploaded_by=self_user.id,
            original_filename="seed.docx",
            content_sha256="c" * 64,
            status="provisioned",
            as_of_time=date(2026, 4, 26),
        )
        session.add(job)
        await session.flush()
        person = LabPerson(
            user_id=self_user.id,
            name="万振凯",
            title="教授",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="f" * 64,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add(person)
        await session.commit()
        person_id = str(person.id)

    # 1) GET before signature exists -> 404 SIGNATURE_NOT_FOUND; non-existent person -> 404 NOT_FOUND
    r_missing = await client.get(
        f"/api/v1/lab/people/{person_id}/signature", headers=viewer_headers
    )
    assert r_missing.status_code == 404
    assert r_missing.json()["detail"]["code"] == "SIGNATURE_NOT_FOUND"

    r_no_person = await client.get(
        f"/api/v1/lab/people/{uuid.uuid4()}/signature", headers=viewer_headers
    )
    assert r_no_person.status_code == 404
    assert r_no_person.json()["detail"]["code"] == "NOT_FOUND"

    # 2) Permission checks: other member and viewer get 403 on PUT/DELETE
    png1 = make_png_bytes(90, 40, fill_byte=0x44)
    for forbidden_hdr in (other_member_headers, viewer_headers):
        r_forb = await client.put(
            f"/api/v1/lab/people/{person_id}/signature",
            headers=forbidden_hdr,
            files={"file": ("sig.png", png1, "image/png")},
        )
        assert r_forb.status_code == 403
        r_del_forb = await client.delete(
            f"/api/v1/lab/people/{person_id}/signature",
            headers=forbidden_hdr,
        )
        assert r_del_forb.status_code == 403

    # 3) Self member uploads signature -> 200
    r_put1 = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=self_headers,
        files={"file": ("sig.png", png1, "image/png")},
    )
    assert r_put1.status_code == 200
    put1_data = r_put1.json()
    sha1 = put1_data["signature_sha256"]
    assert put1_data["has_signature"] is True
    assert put1_data["signature_source"] == "upload"
    assert put1_data["width"] == 90
    assert put1_data["height"] == 40

    # 4) GET signature stream -> verify bytes, ETag, Content-Disposition, nosniff, CSP, and 304 on If-None-Match
    r_get = await client.get(
        f"/api/v1/lab/people/{person_id}/signature", headers=viewer_headers
    )
    assert r_get.status_code == 200
    assert r_get.content == png1
    assert r_get.headers["content-type"] == "image/png"
    assert r_get.headers["etag"] == f'"{sha1}"'
    assert r_get.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r_get.headers["content-security-policy"]
    assert "filename*=UTF-8''" in r_get.headers["content-disposition"]

    r_304 = await client.get(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={**viewer_headers, "If-None-Match": f'"{sha1}"'},
    )
    assert r_304.status_code == 304
    assert r_304.content == b""

    # 5) If-Match optimistic concurrency on PUT: wrong ETag -> 412, matching ETag -> 200
    jpg2 = make_jpeg_bytes(110, 48, seed=7)
    r_412 = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={**admin_headers, "If-Match": '"0000000000000000"'},
        files={"file": ("sig.jpg", jpg2, "image/jpeg")},
    )
    assert r_412.status_code == 412
    assert r_412.json()["detail"]["code"] == "PRECONDITION_FAILED"

    r_put2 = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={**admin_headers, "If-Match": f'"{sha1}"'},
        files={"file": ("sig.jpg", jpg2, "image/jpeg")},
    )
    assert r_put2.status_code == 200
    assert r_put2.json()["signature_mime"] == "image/jpeg"

    # 6) Upload validation errors: 415 SVG, 413 >2MiB, 422 corrupt, 422 bad dimensions
    r_svg = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("evil.png", b"<svg></svg>", "image/png")},
    )
    assert r_svg.status_code == 415
    assert r_svg.json()["detail"]["code"] == "SIGNATURE_REJECTED_FORMAT"

    r_big = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("big.png", b"\x89PNG\r\n\x1a\n" + b"0" * 2_097_150, "image/png")},
    )
    assert r_big.status_code == 413
    assert r_big.json()["detail"]["code"] == "SIGNATURE_TOO_LARGE"

    r_over_multipart = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("huge.png", b"\x89PNG\r\n\x1a\n" + b"0" * 3_000_000, "image/png")},
    )
    assert r_over_multipart.status_code == 413
    assert r_over_multipart.json()["detail"]["code"] == "SIGNATURE_TOO_LARGE"

    async def _chunked_oversized_stream():
        for _ in range(30):
            yield b"x" * 100_000

    r_over_stream = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={
            **admin_headers,
            "Content-Type": "multipart/form-data; boundary=---bound",
        },
        content=_chunked_oversized_stream(),
    )
    assert r_over_stream.status_code == 413
    assert r_over_stream.json()["detail"]["code"] == "SIGNATURE_TOO_LARGE"

    r_missing_file = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        data={"other_field": "no_file"},
    )
    assert r_missing_file.status_code == 422
    assert r_missing_file.json()["detail"]["code"] == "SIGNATURE_FILE_MISSING"

    r_corrupt = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("bad.png", b"\x89PNG\r\n\x1a\ntrun", "image/png")},
    )
    assert r_corrupt.status_code == 422
    assert r_corrupt.json()["detail"]["code"] == "SIGNATURE_CORRUPT"

    r_dim = await client.put(
        f"/api/v1/lab/people/{person_id}/signature",
        headers=admin_headers,
        files={"file": ("tiny.png", make_png_bytes(4, 4), "image/png")},
    )
    assert r_dim.status_code == 422
    assert r_dim.json()["detail"]["code"] == "SIGNATURE_BAD_DIMENSIONS"

    # 7) Verify list_lab_people and get_person_dossier NEVER select signature_data in SQL
    executed_sqls: list[str] = []
    sync_engine = get_engine().sync_engine

    def _capture_sql(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        executed_sqls.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _capture_sql)
    try:
        r_list = await client.get("/api/v1/lab/people", headers=viewer_headers)
        assert r_list.status_code == 200
        assert "signature_data" not in r_list.json()["people"][0]
        assert r_list.json()["people"][0]["has_signature"] is True

        r_dos = await client.get(
            f"/api/v1/lab/people/{person_id}/dossier", headers=viewer_headers
        )
        assert r_dos.status_code == 200
        assert "signature_data" not in r_dos.json()["person"]
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture_sql)

    assert len(executed_sqls) > 0
    for stmt in executed_sqls:
        assert "signature_data" not in stmt

    # 8) DELETE signature (If-Match mismatch -> 412; matching/omitted -> 200, idempotent), subsequent GET -> 404
    r_del_412 = await client.delete(
        f"/api/v1/lab/people/{person_id}/signature",
        headers={**self_headers, "If-Match": '"0000000000000000"'},
    )
    assert r_del_412.status_code == 412
    assert r_del_412.json()["detail"]["code"] == "PRECONDITION_FAILED"

    r_del1 = await client.delete(
        f"/api/v1/lab/people/{person_id}/signature", headers=self_headers
    )
    assert r_del1.status_code == 200
    assert r_del1.json()["has_signature"] is False

    r_del2 = await client.delete(
        f"/api/v1/lab/people/{person_id}/signature", headers=self_headers
    )
    assert r_del2.status_code == 200

    r_after_del = await client.get(
        f"/api/v1/lab/people/{person_id}/signature", headers=viewer_headers
    )
    assert r_after_del.status_code == 404


# ---------------------------------------------------------------------------
# 6. Person Dossier Attribution & Query Budget (§4 & §8.2 test_dossier)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_person_dossier_attribution_and_query_budget(
    client: AsyncClient,
) -> None:
    # Unit-test normalize_person_name and split_names edge cases
    assert normalize_person_name("万 振 凯") == "万振凯"
    assert normalize_person_name("郭　建民") == "郭建民"
    assert normalize_person_name("郭建民（负责人）") == "郭建民"
    assert normalize_person_name("万振凯*") == "万振凯"
    assert normalize_person_name("徐国伟(通讯作者)") == "徐国伟"
    assert normalize_person_name("买买提•艾力") == "买买提·艾力"
    assert split_names("万振凯、郭建民*、徐国伟(通讯作者)、等") == [
        "万振凯",
        "郭建民",
        "徐国伟",
    ]
    assert split_names("Alice Smith and Bob Jones & 张三") == [
        "alicesmith",
        "bobjones",
        "张三",
    ]
    assert split_names("张三、欧阳 娜娜") == ["张三", "欧阳娜娜"]
    assert split_names("Roland, et al.") == ["roland"]

    # Unit-test classify_achievement_for_person preserving original authors_text position
    # when first_author metadata differs from or is absent in authors_text (Spec v2.2 §4.1)
    ach_override_in_list = LabAchievement(
        kind="paper",
        title="一作覆盖但保留原位论文",
        authors_text="张三、李四",
        first_author="李四",
        raw_text="张三、李四: 一作覆盖但保留原位论文",
        parse_quality="structured",
        entity_fingerprint="f1" * 32,
        as_of_time=date(2026, 4, 26),
    )
    assert classify_achievement_for_person(ach_override_in_list, "张三") == (
        "first_author",
        1,
    )
    assert classify_achievement_for_person(ach_override_in_list, "李四") == (
        "first_author",
        2,
    )

    ach_override_outside_list = LabAchievement(
        kind="paper",
        title="列表外一作覆盖不降级不偏移论文",
        authors_text="张三、李四",
        first_author="王五",
        raw_text="张三、李四: 列表外一作覆盖不降级不偏移论文",
        parse_quality="structured",
        entity_fingerprint="f2" * 32,
        as_of_time=date(2026, 4, 26),
    )
    assert classify_achievement_for_person(ach_override_outside_list, "张三") == (
        "first_author",
        1,
    )
    assert classify_achievement_for_person(ach_override_outside_list, "李四") == (
        "coauthor",
        2,
    )
    assert classify_achievement_for_person(ach_override_outside_list, "王五") == (
        "first_author",
        1,
    )

    _, admin_headers = await _seed_user_and_token(role="admin", login_name="dos_admin")
    zhang_user, zhang_headers = await _seed_user_and_token(
        role="member", login_name="zhangsan", display_name="张三"
    )
    _, other_headers = await _seed_user_and_token(
        role="member", login_name="lisi", display_name="李四"
    )

    maker = get_sessionmaker()
    async with maker() as session:
        job = ImportJob(
            uploaded_by=zhang_user.id,
            original_filename="dossier.docx",
            content_sha256="d" * 64,
            status="provisioned",
            as_of_time=date(2026, 4, 26),
        )
        session.add(job)
        await session.flush()

        p_zhang = LabPerson(
            user_id=zhang_user.id,
            name="张三",
            title="教授",
            affiliation="天津工业大学",
            project_role="applicant",
            entity_fingerprint="a1" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        p_zhangsanfeng = LabPerson(
            name="张三丰",
            title="副教授",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="a2" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        p_special = LabPerson(
            name="王%_测试",
            title="讲师",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="a3" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([p_zhang, p_zhangsanfeng, p_special])
        await session.flush()

        # Projects:
        # 1. PI match with annotation & space ("张 三（负责人）") + also listed in participants -> deduplicated as "pi"
        proj_pi = LabProject(
            title="张三主持且在参与人列表的项目",
            funder="国家自然科学基金",
            grant_no="NSFC01",
            pi_name="张 三（负责人）",
            lab_role="host",
            start_date=date(2025, 1, 1),
            status="ongoing",
            status_source="explicit",
            raw_text="《张三主持且在参与人列表的项目》，张 三（负责人）主持，主要参与人：张三、李四",
            entity_fingerprint="b1" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        # 2. Participant match
        proj_part = LabProject(
            title="李四主持张三参与的项目",
            funder="天津市教委",
            grant_no="TJ02",
            pi_name="李四",
            lab_role="participant",
            start_date=date(2026, 1, 1),
            status="ongoing",
            status_source="explicit",
            raw_text="《李四主持张三参与的项目》，李四主持，参与人：王五、张三*、赵六",
            entity_fingerprint="b2" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        # 3. False positive candidate: 张三丰 (must NOT match 张三!)
        proj_false = LabProject(
            title="张三丰主持的项目",
            funder="教育部",
            grant_no="MOE03",
            pi_name="张三丰",
            lab_role="host",
            start_date=date(2026, 3, 1),
            status="ongoing",
            status_source="explicit",
            raw_text="《张三丰主持的项目》，张三丰主持，参与人：张三丰、李四",
            entity_fingerprint="b3" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([proj_pi, proj_part, proj_false])

        # Achievements:
        # 1. First author (2025)
        ach_first = LabAchievement(
            kind="paper",
            title="张三第一作者论文",
            first_author="张 三*",
            authors_text="张 三*, 李四, 王五",
            venue="计算机学报",
            year=2025,
            raw_text="张三(第一):《张三第一作者论文》,2025。",
            parse_quality="structured",
            entity_fingerprint="c1" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        # 2. Coauthor position 3 (2025)
        ach_co = LabAchievement(
            kind="paper",
            title="张三第三作者论文",
            first_author="李四",
            authors_text="李四、王五、张三(通讯作者)、等",
            venue="软件学报",
            year=2025,
            raw_text="李四:《张三第三作者论文》,2025。",
            parse_quality="structured",
            entity_fingerprint="c2" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        # 3. False positive: 张三丰 (must NOT match 张三!)
        ach_false = LabAchievement(
            kind="paper",
            title="张三丰第一作者论文",
            first_author="张三丰",
            authors_text="张三丰, 李四",
            venue="自动化学报",
            year=2026,
            raw_text="张三丰:《张三丰第一作者论文》,2026。",
            parse_quality="structured",
            entity_fingerprint="c3" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([ach_first, ach_co, ach_false])
        await session.commit()
        zhang_id = str(p_zhang.id)
        special_id = str(p_special.id)

    # Measure SQL query count for dossier endpoint (must be <= 4 and never load signature_data)
    executed_sqls: list[str] = []
    sync_engine = get_engine().sync_engine

    def _capture_sql(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        executed_sqls.append(statement)

    event.listen(sync_engine, "before_cursor_execute", _capture_sql)
    try:
        r_dos = await client.get(
            f"/api/v1/lab/people/{zhang_id}/dossier", headers=zhang_headers
        )
    finally:
        event.remove(sync_engine, "before_cursor_execute", _capture_sql)

    assert r_dos.status_code == 200
    dossier_sqls = [
        s
        for s in executed_sqls
        if any(t in s for t in ("lab_people", "lab_projects", "lab_achievements"))
    ]
    assert len(dossier_sqls) <= 4
    assert all("signature_data" not in s for s in executed_sqls)

    dos = r_dos.json()
    assert dos["can_edit"] is True
    assert dos["person"]["can_edit"] is True
    assert dos["name_ambiguous"] is False
    assert dos["person"]["name_ambiguous"] is False
    assert dos["counts"]["projects_total"] == 2
    assert dos["counts"]["projects_as_pi"] == 1
    assert dos["counts"]["projects_as_participant"] == 1
    assert dos["counts"]["outputs_total"] == 2
    assert dos["counts"]["outputs_first_author"] == 1
    assert dos["counts"]["outputs_coauthor"] == 1

    # Verify ordering: PI project first, then participant project; 张三丰 excluded
    assert [p["relation_role"] for p in dos["projects"]] == ["pi", "participant"]
    assert all(p["match"] == "exact" for p in dos["projects"])
    assert all("张三丰" not in p["title"] for p in dos["projects"])

    # Verify outputs ordering: same year 2025 -> first_author before coauthor; 张三丰 excluded
    assert [o["relation_role"] for o in dos["outputs"]] == ["first_author", "coauthor"]
    assert [o["author_position"] for o in dos["outputs"]] == [1, 3]
    assert all(o["match"] == "exact" for o in dos["outputs"])
    assert all("张三丰" not in o["title"] for o in dos["outputs"])

    # Check can_edit=False for other member, and wildcard injection safety for `王%_测试`
    r_other_dos = await client.get(
        f"/api/v1/lab/people/{zhang_id}/dossier", headers=other_headers
    )
    assert r_other_dos.status_code == 200
    assert r_other_dos.json()["can_edit"] is False

    r_spec_dos = await client.get(
        f"/api/v1/lab/people/{special_id}/dossier", headers=admin_headers
    )
    assert r_spec_dos.status_code == 200
    assert r_spec_dos.json()["counts"]["projects_total"] == 0
    assert r_spec_dos.json()["counts"]["outputs_total"] == 0

    r_404 = await client.get(
        f"/api/v1/lab/people/{uuid.uuid4()}/dossier", headers=admin_headers
    )
    assert r_404.status_code == 404

    # Regression check: POST /lab/projects with pi_name="张三" and lab_role="participant" (omitting raw_text)
    # classifies as "participant" in 张三's dossier
    r_create_part_proj = await client.post(
        "/api/v1/lab/projects",
        headers=admin_headers,
        json={
            "title": "手动创建的参与课题（无自由文本）",
            "funder": "天津市科技局",
            "grant_no": "TJ-PART-2026",
            "pi_name": "张三",
            "lab_role": "participant",
            "status": "ongoing",
        },
    )
    assert r_create_part_proj.status_code == 200, r_create_part_proj.text
    r_dos_after_part = await client.get(
        f"/api/v1/lab/people/{zhang_id}/dossier", headers=zhang_headers
    )
    assert r_dos_after_part.status_code == 200
    dos_after_part = r_dos_after_part.json()
    assert dos_after_part["counts"]["projects_as_pi"] == 1
    assert dos_after_part["counts"]["projects_as_participant"] == 2

    # Regression check: Latin/English name with spaces ("John Smith") recalled by SQL ILIKE prefilter and matched
    async with maker() as session:
        p_john = LabPerson(
            name="John Smith",
            title="Visiting Professor",
            affiliation="Tianjin Polytechnic University",
            project_role="member",
            entity_fingerprint="a4" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        proj_john = LabProject(
            title="Cross-Modal Reasoning",
            funder="NSFC",
            grant_no="INT-2026",
            pi_name="john smith",
            lab_role="host",
            status="ongoing",
            status_source="explicit",
            raw_text="Cross-Modal Reasoning, PI: john smith",
            entity_fingerprint="b4" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        ach_john = LabAchievement(
            kind="paper",
            title="Spaced Name Attribution Paper",
            first_author="john smith",
            authors_text="john smith, 张三",
            venue="IEEE TPAMI",
            year=2026,
            raw_text="john smith: Spaced Name Attribution Paper, 2026.",
            parse_quality="structured",
            entity_fingerprint="c4" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([p_john, proj_john, ach_john])
        await session.commit()
        john_id = str(p_john.id)

    r_john_dos = await client.get(
        f"/api/v1/lab/people/{john_id}/dossier", headers=admin_headers
    )
    assert r_john_dos.status_code == 200
    john_dos = r_john_dos.json()
    assert john_dos["counts"]["projects_total"] == 1
    assert john_dos["counts"]["projects_as_pi"] == 1
    assert john_dos["counts"]["outputs_total"] == 1
    assert john_dos["counts"]["outputs_first_author"] == 1

    # Regression check: middle-dot variant ("买买提·艾力" vs "买买提•艾力") and fullwidth Latin variant
    async with maker() as session:
        p_maimaiti = LabPerson(
            name="买买提·艾力",
            title="副教授",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="a6" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        ach_maimaiti = LabAchievement(
            kind="paper",
            title="Middle Dot Variant Attribution Paper",
            first_author="买买提•艾力",
            authors_text="买买提•艾力, 张三",
            venue="自动化学报",
            year=2026,
            raw_text="买买提•艾力: Middle Dot Variant Attribution Paper, 2026.",
            parse_quality="structured",
            entity_fingerprint="c6" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        ach_john_fw = LabAchievement(
            kind="paper",
            title="Fullwidth Latin Author Variant Paper",
            first_author="Ｊｏｈｎ Ｓｍｉｔｈ",
            authors_text="Ｊｏｈｎ Ｓｍｉｔｈ",
            venue="IEEE TKDE",
            year=2026,
            raw_text="Ｊｏｈｎ Ｓｍｉｔｈ: Fullwidth Latin Author Variant Paper, 2026.",
            parse_quality="structured",
            entity_fingerprint="c7" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([p_maimaiti, ach_maimaiti, ach_john_fw])
        await session.commit()
        maimaiti_id = str(p_maimaiti.id)

    r_mmt_dos = await client.get(
        f"/api/v1/lab/people/{maimaiti_id}/dossier", headers=admin_headers
    )
    assert r_mmt_dos.status_code == 200
    assert r_mmt_dos.json()["counts"]["outputs_first_author"] == 1

    r_john_fw_dos = await client.get(
        f"/api/v1/lab/people/{john_id}/dossier", headers=admin_headers
    )
    assert r_john_fw_dos.status_code == 200
    assert r_john_fw_dos.json()["counts"]["outputs_first_author"] == 2

    # Duplicate normalized name ("张 三") triggers name_ambiguous=True and match="ambiguous"
    async with maker() as session:
        p_zhang_dup = LabPerson(
            name="张 三",
            title="讲师",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="a5" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add(p_zhang_dup)
        await session.commit()

    r_ambig_dos = await client.get(
        f"/api/v1/lab/people/{zhang_id}/dossier", headers=zhang_headers
    )
    assert r_ambig_dos.status_code == 200
    ambig_dos = r_ambig_dos.json()
    assert ambig_dos["name_ambiguous"] is True
    assert ambig_dos["person"]["name_ambiguous"] is True
    assert all(p["match"] == "ambiguous" for p in ambig_dos["projects"])
    assert all(o["match"] == "ambiguous" for o in ambig_dos["outputs"])


# ---------------------------------------------------------------------------
# 7. Proposal Reuse Basket: Batch Format, Signatures ZIP, Selected .docx (§6 & §8.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basket_format_zip_and_selected_docx_export(
    client: AsyncClient,
) -> None:
    user, admin_headers = await _seed_user_and_token(role="admin", login_name="basket_admin")
    maker = get_sessionmaker()

    # Wide PNG (600x50, 12:1 aspect ratio) to verify proportional width+height bounding (<=1.5in x <=0.45in)
    png_bytes = make_png_bytes(600, 50, fill_byte=0x22)
    jpg_bytes = make_jpeg_bytes(100, 42, seed=5)
    webp_bytes = make_webp_vp8_bytes(80, 40)

    async with maker() as session:
        job = ImportJob(
            uploaded_by=user.id,
            original_filename="basket.docx",
            content_sha256="e" * 64,
            status="provisioned",
            as_of_time=date(2026, 4, 26),
        )
        session.add(job)
        await session.flush()

        p1 = LabPerson(
            name="郭建民",
            title="正高级实验师",
            affiliation="天津工业大学",
            project_role="applicant",
            bio="郭建民学术简历段落。",
            signature_data=jpg_bytes,
            signature_mime="image/jpeg",
            signature_sha256=hashlib.sha256(jpg_bytes).hexdigest(),
            signature_updated_at=datetime.now(UTC),
            signature_source="docx_import",
            entity_fingerprint="d1" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        p2 = LabPerson(
            name="万振凯",
            title="教授",
            affiliation="天津工业大学",
            project_role="member",
            signature_data=png_bytes,
            signature_mime="image/png",
            signature_sha256=hashlib.sha256(png_bytes).hexdigest(),
            signature_updated_at=datetime.now(UTC),
            signature_source="docx_import",
            entity_fingerprint="d2" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        p3_same_name = LabPerson(
            name="万振凯",
            title="讲师",
            affiliation="天津工业大学软件学院",
            project_role="member",
            signature_data=webp_bytes,
            signature_mime="image/webp",
            signature_sha256=hashlib.sha256(webp_bytes).hexdigest(),
            signature_updated_at=datetime.now(UTC),
            signature_source="upload",
            entity_fingerprint="d3" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        p4_no_sig = LabPerson(
            name="刘岩",
            title="讲师",
            affiliation="天津工业大学",
            project_role="member",
            entity_fingerprint="d4" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        p5_corrupt = LabPerson(
            name="王秋慧",
            title="教授",
            affiliation="天津工业大学",
            project_role="member",
            signature_data=b"\x89PNG\r\n\x1a\ncorrupt_payload",
            signature_mime="image/png",
            signature_sha256="f" * 64,
            signature_updated_at=datetime.now(UTC),
            signature_source="docx_import",
            entity_fingerprint="d5" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        proj = LabProject(
            title="智能评测关键技术研究",
            funder="天津市教委",
            grant_no="AGE240005",
            pi_name="郭建民",
            lab_role="host",
            start_date=date(2024, 12, 1),
            status="ongoing",
            status_source="explicit",
            raw_text="《智能评测关键技术研究》(AGE240005),郭建民主持,天津市教委,2024年12月立项,在研。",
            entity_fingerprint="d6" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        ach = LabAchievement(
            kind="report",
            title="高等教育高质量发展规划设计研究",
            first_author="王秋慧",
            authors_text="王秋慧, 郭建民",
            venue="天津市教委",
            year=2025,
            support_note="该成果可支撑课题政策研究。",
            raw_text="王秋慧(第一):《高等教育高质量发展规划设计研究》,2025。",
            parse_quality="structured",
            entity_fingerprint="d7" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        fac = LabFacility(
            category="equipment",
            name="大模型训练服务器",
            spec="8x H800",
            value_wan=260,
            raw_text="大模型训练服务器（8x H800）",
            entity_fingerprint="d8" * 32,
            as_of_time=date(2026, 4, 26),
            source_import_id=job.id,
        )
        session.add_all([p1, p2, p3_same_name, p4_no_sig, p5_corrupt, proj, ach, fac])
        await session.commit()
        p1_id, p2_id, p3_id, p4_id, p5_id = (
            str(p1.id),
            str(p2.id),
            str(p3_same_name.id),
            str(p4_no_sig.id),
            str(p5_corrupt.id),
        )
        proj_id, ach_id, fac_id = str(proj.id), str(ach.id), str(fac.id)

    # 1) POST /api/v1/lab/format/batch (grouped vs ungrouped, numbering styles, missing ID -> 404, limit -> 400)
    basket_items = [
        {"entity": "person", "id": p1_id},
        {"entity": "project", "id": proj_id},
        {"entity": "output", "id": ach_id},
        {"entity": "facility", "id": fac_id},
    ]
    r_fmt = await client.post(
        "/api/v1/lab/format/batch",
        headers=admin_headers,
        json={
            "items": basket_items,
            "options": {
                "style": "nsfc_standard",
                "numbering": "bracket",
                "group_by_entity": True,
                "include_person_bio": True,
                "include_support_note": True,
            },
        },
    )
    assert r_fmt.status_code == 200
    fmt_data = r_fmt.json()
    assert len(fmt_data["items"]) == 4
    assert "一、项目组成员" in fmt_data["combined_text"]
    assert "二、承担项目" in fmt_data["combined_text"]
    assert "三、前期研究成果" in fmt_data["combined_text"]
    assert "四、基础条件与设备" in fmt_data["combined_text"]
    assert "郭建民学术简历段落" in fmt_data["combined_text"]

    # Ungrouped with paren numbering
    r_fmt_ungrouped = await client.post(
        "/api/v1/lab/format/batch",
        headers=admin_headers,
        json={
            "items": basket_items,
            "options": {"numbering": "paren", "group_by_entity": False},
        },
    )
    assert r_fmt_ungrouped.status_code == 200
    assert "（1）" in r_fmt_ungrouped.json()["combined_text"]
    assert "（4）" in r_fmt_ungrouped.json()["combined_text"]

    # Missing ID -> 404 with missing list
    r_fmt_missing = await client.post(
        "/api/v1/lab/format/batch",
        headers=admin_headers,
        json={
            "items": [
                {"entity": "person", "id": p1_id},
                {"entity": "project", "id": 99},
            ]
        },
    )
    assert r_fmt_missing.status_code == 404
    assert r_fmt_missing.json()["detail"]["code"] == "NOT_FOUND"
    assert r_fmt_missing.json()["detail"]["missing"] == [{"entity": "project", "id": 99}]

    # Duplicate entries (including entity aliases like person/people and output/achievement) are deduplicated in first-seen order
    dup_basket_items = [
        {"entity": "person", "id": p1_id},
        {"entity": "people", "id": f"  {p1_id.upper()}  "},
        {"entity": "project", "id": proj_id},
        {"entity": "projects", "id": proj_id},
        {"entity": "output", "id": ach_id},
        {"entity": "achievement", "id": ach_id},
    ]
    r_fmt_dedup = await client.post(
        "/api/v1/lab/format/batch",
        headers=admin_headers,
        json={
            "items": dup_basket_items,
            "options": {"numbering": "bracket", "group_by_entity": True},
        },
    )
    assert r_fmt_dedup.status_code == 200
    dedup_data = r_fmt_dedup.json()
    assert [(it["entity"], it["id"]) for it in dedup_data["items"]] == [
        ("person", p1_id),
        ("project", proj_id),
        ("output", ach_id),
    ]
    assert "[2]" not in dedup_data["combined_text"]

    # Over 200 distinct items -> 400 BATCH_LIMIT_EXCEEDED
    r_fmt_over = await client.post(
        "/api/v1/lab/format/batch",
        headers=admin_headers,
        json={
            "items": [{"entity": "person", "id": str(uuid.uuid4())} for _ in range(201)]
        },
    )
    assert r_fmt_over.status_code == 400
    assert r_fmt_over.json()["detail"]["code"] == "BATCH_LIMIT_EXCEEDED"

    # 2) POST /api/v1/lab/people/signatures:zip (included, missing, not_found, corrupt, duplicate name _2)
    r_zip = await client.post(
        "/api/v1/lab/people/signatures:zip",
        headers=admin_headers,
        json={
            "person_ids": [p1_id, p2_id, p3_id, p4_id, p5_id, 99],
            "filename_template": "name",
        },
    )
    assert r_zip.status_code == 200
    assert r_zip.headers["x-signature-count-included"] == "3"
    assert r_zip.headers["x-signature-count-missing"] == "3"
    with zipfile.ZipFile(io.BytesIO(r_zip.content)) as zf:
        names = set(zf.namelist())
        assert "郭建民.jpg" in names
        assert "万振凯.png" in names
        assert "万振凯.webp" in names
        assert "manifest.csv" in names
        manifest_raw = zf.read("manifest.csv").decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(manifest_raw)))
        assert [r["status"] for r in rows] == [
            "included",
            "included",
            "included",
            "missing",
            "corrupt",
            "not_found",
        ]

    # Same extension collision disambiguation (_2)
    async with maker() as session:
        p3_obj = await session.get(LabPerson, uuid.UUID(p3_id))
        assert p3_obj is not None
        p3_obj.signature_data = png_bytes
        p3_obj.signature_mime = "image/png"
        p3_obj.signature_sha256 = hashlib.sha256(png_bytes).hexdigest()
        await session.commit()

    r_zip_collision = await client.post(
        "/api/v1/lab/people/signatures:zip",
        headers=admin_headers,
        json={"person_ids": [p2_id, p3_id], "filename_template": "name"},
    )
    assert r_zip_collision.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r_zip_collision.content)) as zf:
        assert "万振凯.png" in zf.namelist()
        assert "万振凯_2.png" in zf.namelist()

    # Restore p3 back to WebP for .docx export test
    async with maker() as session:
        p3_obj = await session.get(LabPerson, uuid.UUID(p3_id))
        assert p3_obj is not None
        p3_obj.signature_data = webp_bytes
        p3_obj.signature_mime = "image/webp"
        p3_obj.signature_sha256 = hashlib.sha256(webp_bytes).hexdigest()
        await session.commit()

    # 3) POST /api/v1/lab/export:selected (with annex=False and annex=True including PNG/JPEG/WebP/missing/corrupt)
    r_doc_no_annex = await client.post(
        "/api/v1/lab/export:selected",
        headers=admin_headers,
        json={
            "title": "课题申报素材汇编",
            "items": [
                *basket_items,
                {"entity": "people", "id": p1_id},
                {"entity": "projects", "id": proj_id},
            ],
            "options": {
                "style": "nsfc_standard",
                "numbering": "bracket",
                "include_person_bio": True,
                "include_support_note": True,
                "include_signatures_annex": False,
            },
        },
    )
    assert r_doc_no_annex.status_code == 200
    doc_no = docx.Document(io.BytesIO(r_doc_no_annex.content))
    assert len(doc_no.tables) == 0
    doc_no_text = "\n".join(p.text for p in doc_no.paragraphs)
    assert "共 4 条素材" in doc_no_text
    assert "[2]" not in doc_no_text
    for item_obj in fmt_data["items"]:
        assert str(item_obj["text"]) in doc_no_text

    # With include_signatures_annex=True and all 5 people (JPEG, PNG, WebP, missing, corrupt)
    r_doc_annex = await client.post(
        "/api/v1/lab/export:selected",
        headers=admin_headers,
        json={
            "title": "含签名附件汇编",
            "items": [
                {"entity": "person", "id": p1_id},
                {"entity": "person", "id": p2_id},
                {"entity": "person", "id": p3_id},
                {"entity": "person", "id": p4_id},
                {"entity": "person", "id": p5_id},
                {"entity": "project", "id": proj_id},
            ],
            "options": {"include_signatures_annex": True},
        },
    )
    assert r_doc_annex.status_code == 200
    # WebP (p3) + corrupt (p5) = 2 warnings
    assert r_doc_annex.headers["x-export-warnings-count"] == "2"
    doc_annex = docx.Document(io.BytesIO(r_doc_annex.content))
    assert len(doc_annex.tables) == 1
    sig_table = doc_annex.tables[0]
    assert len(sig_table.rows) == 6  # 1 header + 5 people
    assert sig_table.rows[3].cells[3].text == "（WebP 签名请通过签名包导出）"
    assert sig_table.rows[4].cells[3].text == "（未录入）"
    assert sig_table.rows[5].cells[3].text == "（签名数据异常，已跳过）"
    # Verify p1 (JPEG) and p2 (PNG) embedded <a:blip> in XML and bounded inline shape dimensions
    blip_elems = [el for el in sig_table._tbl.iter() if el.tag.endswith("}blip")]
    assert len(blip_elems) == 2
    assert len(doc_annex.inline_shapes) == 2
    for shape in doc_annex.inline_shapes:
        assert shape.width <= docx.shared.Inches(1.51)
        assert shape.height <= docx.shared.Inches(0.46)

    # 4) Verify lab.signature.read audit events recorded for single, zip, and docx_export channels
    r_single_sig = await client.get(
        f"/api/v1/lab/people/{p1_id}/signature", headers=admin_headers
    )
    assert r_single_sig.status_code == 200
    async with maker() as session:
        read_audits = list(
            await session.scalars(
                select(AuditEvent).where(AuditEvent.action == "lab.signature.read")
            )
        )
    channels = {str((ev.detail or {}).get("channel")) for ev in read_audits}
    assert {"single", "zip", "docx_export"}.issubset(channels)
    for ev in read_audits:
        assert "person_id" in (ev.detail or {})
        assert "sha256" in (ev.detail or {})
        assert "signature_data" not in (ev.detail or {})

    # 5) Verify XML illegal control characters (\u000b, \x07) in title or entity text do not cause 500
    async with maker() as session:
        proj_obj = await session.get(LabProject, uuid.UUID(proj_id))
        assert proj_obj is not None
        proj_obj.title = "智能评测\x0b关键技术\x07研究"
        await session.commit()

    r_doc_ctrl = await client.post(
        "/api/v1/lab/export:selected",
        headers=admin_headers,
        json={
            "title": "课题申报\x0b素材\x07汇编",
            "items": [{"entity": "project", "id": proj_id}],
            "options": {"include_signatures_annex": False},
        },
    )
    assert r_doc_ctrl.status_code == 200
    doc_ctrl = docx.Document(io.BytesIO(r_doc_ctrl.content))
    ctrl_text = "\n".join(p.text for p in doc_ctrl.paragraphs)
    assert "\x0b" not in ctrl_text and "\x07" not in ctrl_text
    assert "课题申报素材汇编" in ctrl_text
    assert "智能评测关键技术研究" in ctrl_text
