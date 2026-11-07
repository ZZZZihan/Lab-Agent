from __future__ import annotations

import csv
import hashlib
import io
import uuid
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import docx
import pytest
from app.db import get_sessionmaker
from app.models import (
    AuditEvent,
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
)
from httpx import AsyncClient
from sqlalchemy import select
from tests.lab_v22_helpers import (
    _seed_user_and_token,
    make_jpeg_bytes,
    make_png_bytes,
    make_webp_vp8_bytes,
)

REAL_SAMPLE_DOCX = Path(
    "/home/carryz/.local/share/alma/herdr/uploads/20261106-054942-6cb60859/"
    "教育考试专属大模型构建与安全应用研究_申请书.docx"
)


# ---------------------------------------------------------------------------
# Pure-Python Synthetic Image & .docx Builders
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
            relationship_note="为本课题提供算法基础。",
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
    assert "为本课题提供算法基础。" in fmt_data["combined_text"]

    r_fmt_no_rel = await client.post(
        "/api/v1/lab/format/batch",
        headers=admin_headers,
        json={
            "items": [{"entity": "project", "id": proj_id}],
            "options": {
                "include_support_note": True,
                "include_relationship_note": False,
            },
        },
    )
    assert r_fmt_no_rel.status_code == 200
    assert "为本课题提供算法基础。" not in r_fmt_no_rel.json()["combined_text"]

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
