"""Comprehensive v2.2 tests for Lab Person Dossier, Electronic Signatures, and Proposal Reuse Basket (Spec v2.2 §8.2)."""

from __future__ import annotations

import io
import re
import struct
import uuid
import zipfile
import zlib
from pathlib import Path

import docx
from app.auth import hash_password, issue_token
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import (
    User,
)

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


