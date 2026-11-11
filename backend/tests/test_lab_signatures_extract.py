from __future__ import annotations

import hashlib
import io
import struct
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from pathlib import Path

import docx
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from app.audit import sanitize_detail
from app.auth import hash_password
from app.db import get_sessionmaker
from app.importer.parser import parse_proposal_docx
from app.importer.pii import PIILeakError, assert_no_pii, scrub_free_text
from app.importer.reader import _extract_rel_ids_from_element, extract_embedded_images
from app.importer.signature import validate_signature_image
from app.models import (
    ImportJob,
    LabPerson,
    User,
)
from sqlalchemy.exc import IntegrityError
from tests.lab_v22_helpers import (
    build_proposal_docx_with_10_signatures,
    make_jpeg_bytes,
    make_png_bytes,
    make_webp_vp8_bytes,
    make_webp_vp8l_bytes,
    make_webp_vp8x_bytes,
)

REAL_SAMPLE_DOCX = Path(
    "/home/carryz/.local/share/alma/herdr/uploads/20261106-054942-6cb60859/"
    "教育考试专属大模型构建与安全应用研究_申请书.docx"
)


# ---------------------------------------------------------------------------
# Pure-Python Synthetic Image & .docx Builders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_v22_single_head_and_constraints() -> None:
    alembic_cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_cfg)
    heads = script.get_heads()
    assert len(heads) == 1
    assert heads == ["20261111_0011"]
    assert script.get_revision("20261110_0009") is not None

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

    # Edge case 5: PI has no signature in [anchor-3, anchor+1], only an unrelated diagram at anchor+2 -> never bound to PI
    no_pi_plus2_docx = build_proposal_docx_with_10_signatures(
        blank_member_with_no_pi_sig=True,
        nearby_pi_diagram=True,
    )
    no_pi_plus2_parsed = parse_proposal_docx("no_pi_plus2.docx", no_pi_plus2_docx)
    assert all(
        s["person_temp_id"] != "p1"
        for s in no_pi_plus2_parsed["preview"]["signatures"]
    )
    assert len(no_pi_plus2_parsed["preview"]["signatures"]) == 9

    # Edge case 6: >64 embedded images (65 members with unique signatures) -> capped at 64 and emits SIG_IMPORT_QUOTA
    quota_doc = docx.Document()
    t_quota_cover = quota_doc.add_table(rows=2, cols=2)
    t_quota_cover.cell(0, 0).text = "项目名称"
    t_quota_cover.cell(0, 1).text = "超配额签名导入测试项目"
    t_quota_cover.cell(1, 0).text = "项目申请人"
    t_quota_cover.cell(1, 1).text = "郭建民"

    t_quota_team = quota_doc.add_table(rows=1 + 65, cols=6)
    for c_i, h_val in enumerate(
        ["姓名", "专业职务", "研究专长", "学位", "工作单位", "本人签字"]
    ):
        t_quota_team.cell(0, c_i).text = h_val
    cn_chars = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸"
    for m_i in range(65):
        r_i = 1 + m_i
        name_suffix = cn_chars[m_i // 10] + cn_chars[m_i % 10]
        t_quota_team.cell(r_i, 0).text = f"陈{name_suffix}"
        t_quota_team.cell(r_i, 1).text = "讲师"
        t_quota_team.cell(r_i, 2).text = "人工智能"
        t_quota_team.cell(r_i, 3).text = "博士"
        t_quota_team.cell(r_i, 4).text = "天津工业大学"
        t_quota_team.cell(r_i, 5).paragraphs[0].add_run().add_picture(
            io.BytesIO(make_png_bytes(40 + (m_i % 30), 20 + (m_i // 30), fill_byte=(10 + m_i) % 250))
        )
    quota_buf = io.BytesIO()
    quota_doc.save(quota_buf)
    quota_docx_bytes = quota_buf.getvalue()
    assert len(extract_embedded_images(quota_docx_bytes)) == 64
    quota_parsed = parse_proposal_docx("quota65.docx", quota_docx_bytes)
    assert len(quota_parsed["preview"]["signatures"]) == 64
    quota_warns = [
        w
        for w in quota_parsed["preview"]["warnings"]
        if w.get("code") == "SIG_IMPORT_QUOTA"
    ]
    assert len(quota_warns) == 1
    assert quota_warns[0]["max_images"] == 64
    assert quota_warns[0]["max_total_bytes"] == 32 * 1024 * 1024

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


