"""Proposal reuse basket services: batch formatting, streaming signature ZIP, and selected .docx export (Spec v2.2 §6)."""

from __future__ import annotations

import csv
import io
import re
import uuid
import zipfile
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import docx
from docx.shared import Inches
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.importer.formatters import (
    format_achievement_application,
    format_facility_application,
    format_person_application,
    format_project_application,
)
from app.importer.pii import (
    PIILeakError,
    assert_no_pii,
    escape_csv_cell,
    scrub_free_text,
)
from app.importer.signature import validate_signature_image
from app.models import LabAchievement, LabFacility, LabPerson, LabProject

MAX_BATCH_ITEMS = 200
MAX_ZIP_PERSONS = 50
MAX_ZIP_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MiB
MAX_DOCX_SIG_BYTES = 25 * 1024 * 1024  # 25 MiB

MIME_TO_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

SAFE_FILENAME_CHAR_RE = re.compile(r"[^一-龥A-Za-z0-9_-]+")
XML_ILLEGAL_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+")


def sanitize_xml_text(text: str | None) -> str:
    """Remove XML 1.0 illegal control characters while preserving \\t, \\n, \\r (Spec v2.2 §5.0)."""
    if not text:
        return ""
    return XML_ILLEGAL_CTRL_RE.sub("", text)

SECTION_ORDER: tuple[tuple[str, str], ...] = (
    ("person", "一、项目组成员"),
    ("project", "二、承担项目"),
    ("output", "三、前期研究成果"),
    ("facility", "四、基础条件与设备"),
)


class BasketError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        missing: list[dict[str, object]] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.missing = missing


def _escape_csv_cell(value: object) -> str:
    return escape_csv_cell(value)


def sanitize_filename_part(raw_name: str | None) -> str:
    """Sanitize a person name for safe archive filenames (Spec v2.2 §5.1 / §6.2)."""
    cleaned = SAFE_FILENAME_CHAR_RE.sub("_", (raw_name or "").strip())
    cleaned = cleaned.strip("._ ")
    cleaned = cleaned[:40].strip("._ ")
    return cleaned or "person"


def build_rfc5987_content_disposition(ascii_fallback: str, utf8_filename: str) -> str:
    """Build RFC 5987 dual Content-Disposition header value."""
    encoded = quote(utf8_filename, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


def _canonical_entity_kind(raw_entity: str) -> str | None:
    lowered = (raw_entity or "").strip().lower()
    if lowered in ("person", "people"):
        return "person"
    if lowered in ("project", "projects"):
        return "project"
    if lowered in ("output", "outputs", "achievement", "achievements"):
        return "output"
    if lowered in ("facility", "facilities"):
        return "facility"
    return None


def _try_parse_uuid(raw_id: object) -> uuid.UUID | None:
    if isinstance(raw_id, uuid.UUID):
        return raw_id
    if isinstance(raw_id, str):
        try:
            return uuid.UUID(raw_id.strip())
        except ValueError:
            return None
    return None


def _format_number_prefix(
    idx: int, numbering: Literal["bracket", "paren", "decimal", "none"] | str
) -> str:
    if numbering == "bracket":
        return f"[{idx}] "
    if numbering == "paren":
        return f"（{idx}）"
    if numbering == "decimal":
        return f"{idx}. "
    return ""


def _format_single_entity(
    canon_kind: str,
    entity_obj: Any,
    *,
    style: str = "nsfc_standard",
    include_person_bio: bool = False,
    include_support_note: bool = True,
    include_relationship_note: bool | None = None,
) -> str:
    if canon_kind == "person":
        return sanitize_xml_text(
            format_person_application(entity_obj, include_bio=include_person_bio)
        )
    if canon_kind == "project":
        if style == "plain":
            return sanitize_xml_text(
                format_project_application(entity_obj, include_relationship=False)
            )
        inc_rel = (
            include_support_note
            if include_relationship_note is None
            else include_relationship_note
        )
        return sanitize_xml_text(
            format_project_application(entity_obj, include_relationship=inc_rel)
        )
    if canon_kind == "output":
        if style == "plain":
            return sanitize_xml_text(
                format_achievement_application(entity_obj, include_support=False)
            )
        return sanitize_xml_text(
            format_achievement_application(
                entity_obj, include_support=include_support_note
            )
        )
    if canon_kind == "facility":
        return sanitize_xml_text(format_facility_application(entity_obj))
    return ""


async def fetch_and_format_batch_items(
    session: AsyncSession,
    *,
    raw_items: Sequence[dict[str, Any]],
    style: str = "nsfc_standard",
    numbering: str = "bracket",
    group_by_entity: bool = True,
    include_person_bio: bool = False,
    include_support_note: bool = True,
    include_relationship_note: bool | None = None,
) -> dict[str, object]:
    """Fetch requested entities in <= 4 SQL queries (never loading signature_data) and format them (Spec v2.2 §6.1)."""
    uuids_by_kind: dict[str, set[uuid.UUID]] = {
        "person": set(),
        "project": set(),
        "output": set(),
        "facility": set(),
    }
    seen_keys: set[tuple[str, str]] = set()
    parsed_entries: list[tuple[str, str | None, object, uuid.UUID | None]] = []

    for item in raw_items:
        orig_entity = str(item.get("entity") or "")
        canon = _canonical_entity_kind(orig_entity)
        orig_id = item.get("id")
        uid = _try_parse_uuid(orig_id)
        dedup_key = (
            (canon, str(uid))
            if canon is not None and uid is not None
            else (f"raw:{orig_entity.strip().lower()}", f"raw:{orig_id}")
        )
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        if canon is not None and uid is not None:
            uuids_by_kind[canon].add(uid)
        parsed_entries.append((orig_entity, canon, orig_id, uid))

    if len(parsed_entries) == 0 or len(parsed_entries) > MAX_BATCH_ITEMS:
        raise BasketError(
            400,
            "BATCH_LIMIT_EXCEEDED",
            f"单次批量格式化素材数量须在 1..{MAX_BATCH_ITEMS} 之间",
        )

    # Execute at most 1 batch query per non-empty entity kind (<= 4 queries total, signature_data stays deferred)
    people_map: dict[uuid.UUID, LabPerson] = {}
    if uuids_by_kind["person"]:
        rows = await session.scalars(
            select(LabPerson).where(LabPerson.id.in_(uuids_by_kind["person"]))
        )
        people_map = {r.id: r for r in rows}

    projects_map: dict[uuid.UUID, LabProject] = {}
    if uuids_by_kind["project"]:
        rows_proj = await session.scalars(
            select(LabProject).where(LabProject.id.in_(uuids_by_kind["project"]))
        )
        projects_map = {r.id: r for r in rows_proj}

    outputs_map: dict[uuid.UUID, LabAchievement] = {}
    if uuids_by_kind["output"]:
        rows_ach = await session.scalars(
            select(LabAchievement).where(LabAchievement.id.in_(uuids_by_kind["output"]))
        )
        outputs_map = {r.id: r for r in rows_ach}

    facilities_map: dict[uuid.UUID, LabFacility] = {}
    if uuids_by_kind["facility"]:
        rows_fac = await session.scalars(
            select(LabFacility).where(LabFacility.id.in_(uuids_by_kind["facility"]))
        )
        facilities_map = {r.id: r for r in rows_fac}

    lookup_by_kind: dict[str, dict[uuid.UUID, Any]] = {
        "person": people_map,
        "project": projects_map,
        "output": outputs_map,
        "facility": facilities_map,
    }

    missing: list[dict[str, object]] = []
    resolved: list[tuple[str, str, object, Any]] = []
    for orig_entity, canon, orig_id, uid in parsed_entries:
        if canon is None or uid is None or uid not in lookup_by_kind[canon]:
            missing.append({"entity": orig_entity, "id": orig_id})
        else:
            resolved.append((orig_entity, canon, orig_id, lookup_by_kind[canon][uid]))

    if missing:
        raise BasketError(
            404,
            "NOT_FOUND",
            "部分素材不存在或已删除",
            missing=missing,
        )

    formatted_items: list[dict[str, object]] = []
    for orig_entity, canon, orig_id, ent_obj in resolved:
        base_text = _format_single_entity(
            canon,
            ent_obj,
            style=style,
            include_person_bio=include_person_bio,
            include_support_note=include_support_note,
            include_relationship_note=include_relationship_note,
        )
        formatted_items.append(
            {
                "entity": orig_entity,
                "canonical_entity": canon,
                "id": orig_id,
                "text": base_text,
                "_obj": ent_obj,
            }
        )

    sections: list[dict[str, object]] = []
    if group_by_entity:
        section_blocks: list[str] = []
        for canon_key, heading in SECTION_ORDER:
            group_entries = [
                it for it in formatted_items if it["canonical_entity"] == canon_key
            ]
            if not group_entries:
                continue
            numbered_lines: list[str] = []
            for idx, it in enumerate(group_entries, start=1):
                prefix = _format_number_prefix(idx, numbering)
                line = f"{prefix}{it['text']}"
                it["numbered_text"] = line
                numbered_lines.append(line)
            sec_text = f"{heading}\n" + "\n".join(numbered_lines)
            section_blocks.append(sec_text)
            sections.append(
                {
                    "entity": canon_key,
                    "heading": heading,
                    "lines": numbered_lines,
                }
            )
        combined_text = "\n\n".join(section_blocks)
    else:
        numbered_lines = []
        for idx, it in enumerate(formatted_items, start=1):
            prefix = _format_number_prefix(idx, numbering)
            line = f"{prefix}{it['text']}"
            it["numbered_text"] = line
            numbered_lines.append(line)
        combined_text = "\n".join(numbered_lines)
        sections.append(
            {
                "entity": "all",
                "heading": None,
                "lines": numbered_lines,
            }
        )

    public_items = [
        {
            "entity": it["entity"],
            "id": it["id"],
            "text": it["text"],
        }
        for it in formatted_items
    ]

    return {
        "items": public_items,
        "combined_text": combined_text,
        "_internal_items": formatted_items,
        "_sections": sections,
    }


async def prepare_signatures_zip(
    session: AsyncSession,
    *,
    raw_person_ids: Sequence[object],
    filename_template: Literal["seq_name", "name", "name_id"] | str = "seq_name",
) -> tuple[bytes, int, int, list[dict[str, str]]]:
    """Build an uncompressed (ZIP_STORED) signature archive with UTF-8 BOM manifest.csv (Spec v2.2 §6.2).

    Returns `(zip_bytes, included_count, missing_or_skipped_count, included_signatures)`.
    """
    if len(raw_person_ids) == 0 or len(raw_person_ids) > MAX_ZIP_PERSONS:
        raise BasketError(
            400,
            "ZIP_PERSON_LIMIT_EXCEEDED",
            f"单次打包签名人数须在 1..{MAX_ZIP_PERSONS} 之间",
        )
    if filename_template not in ("seq_name", "name", "name_id"):
        raise BasketError(
            400,
            "INVALID_FILENAME_TEMPLATE",
            "filename_template 仅支持 seq_name / name / name_id",
        )

    # Deduplicate while preserving request order
    deduped_ids: list[tuple[object, uuid.UUID | None]] = []
    seen_keys: set[str] = set()
    valid_uuids: set[uuid.UUID] = set()
    for raw_id in raw_person_ids:
        uid = _try_parse_uuid(raw_id)
        dedup_key = str(uid) if uid is not None else f"raw:{raw_id}"
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        deduped_ids.append((raw_id, uid))
        if uid is not None:
            valid_uuids.add(uid)

    people_by_id: dict[uuid.UUID, LabPerson] = {}
    if valid_uuids:
        rows = await session.scalars(
            select(LabPerson)
            .options(undefer(LabPerson.signature_data))
            .where(LabPerson.id.in_(valid_uuids))
        )
        people_by_id = {r.id: r for r in rows}

    zip_buf = io.BytesIO()
    manifest_rows: list[list[str]] = []
    used_filenames: set[str] = {"manifest.csv"}
    total_image_bytes = 0
    included_count = 0
    missing_count = 0
    included_signatures: list[dict[str, str]] = []

    with zipfile.ZipFile(zip_buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for seq, (raw_id, uid) in enumerate(deduped_ids, start=1):
            person = people_by_id.get(uid) if uid is not None else None
            if person is None:
                missing_count += 1
                manifest_rows.append(
                    [
                        str(seq),
                        _escape_csv_cell(raw_id),
                        "",
                        "",
                        "",
                        "not_found",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ]
                )
                continue

            role_str = "pi" if person.project_role == "applicant" else "member"
            org_str = person.affiliation or ""
            sig_bytes = person.signature_data

            if sig_bytes is None:
                missing_count += 1
                manifest_rows.append(
                    [
                        str(seq),
                        _escape_csv_cell(raw_id),
                        _escape_csv_cell(person.name),
                        _escape_csv_cell(role_str),
                        _escape_csv_cell(org_str),
                        "missing",
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                    ]
                )
                continue

            check = validate_signature_image(sig_bytes)
            if not check.ok or check.mime is None or check.sha256 is None:
                missing_count += 1
                manifest_rows.append(
                    [
                        str(seq),
                        _escape_csv_cell(raw_id),
                        _escape_csv_cell(person.name),
                        _escape_csv_cell(role_str),
                        _escape_csv_cell(org_str),
                        "corrupt",
                        "",
                        _escape_csv_cell(person.signature_mime or ""),
                        str(len(sig_bytes)),
                        _escape_csv_cell(person.signature_sha256 or ""),
                        _escape_csv_cell(person.signature_source or ""),
                        person.signature_updated_at.isoformat()
                        if person.signature_updated_at
                        else "",
                    ]
                )
                continue

            total_image_bytes += check.size
            if total_image_bytes > MAX_ZIP_TOTAL_BYTES:
                raise BasketError(
                    413,
                    "ZIP_SIZE_LIMIT_EXCEEDED",
                    "签名包总大小超过 50 MiB 上限",
                )

            ext = MIME_TO_EXT.get(check.mime, "bin")
            safe_name = sanitize_filename_part(person.name)
            if filename_template == "seq_name":
                stem = f"{seq:02d}_{safe_name}"
            elif filename_template == "name":
                stem = safe_name
            else:
                stem = f"{safe_name}_{raw_id}"

            candidate_name = f"{stem}.{ext}"
            suffix_idx = 2
            while candidate_name in used_filenames:
                candidate_name = f"{stem}_{suffix_idx}.{ext}"
                suffix_idx += 1
            used_filenames.add(candidate_name)

            zf.writestr(candidate_name, sig_bytes, compress_type=zipfile.ZIP_STORED)
            included_count += 1
            included_signatures.append(
                {"person_id": str(person.id), "sha256": check.sha256}
            )
            manifest_rows.append(
                [
                    str(seq),
                    _escape_csv_cell(raw_id),
                    _escape_csv_cell(person.name),
                    _escape_csv_cell(role_str),
                    _escape_csv_cell(org_str),
                    "included",
                    _escape_csv_cell(candidate_name),
                    check.mime,
                    str(check.size),
                    check.sha256,
                    _escape_csv_cell(person.signature_source or ""),
                    person.signature_updated_at.isoformat()
                    if person.signature_updated_at
                    else "",
                ]
            )

        csv_text_buf = io.StringIO()
        csv_text_buf.write("﻿")
        writer = csv.writer(csv_text_buf)
        writer.writerow(
            [
                "seq",
                "person_id",
                "name",
                "role",
                "org",
                "status",
                "filename",
                "mime",
                "size_bytes",
                "sha256",
                "source",
                "updated_at",
            ]
        )
        writer.writerows(manifest_rows)
        zf.writestr(
            "manifest.csv",
            csv_text_buf.getvalue().encode("utf-8"),
            compress_type=zipfile.ZIP_STORED,
        )

    return zip_buf.getvalue(), included_count, missing_count, included_signatures


async def iter_bytes_chunks(
    data: bytes, chunk_size: int = 65536
) -> AsyncIterator[bytes]:
    """Stream bytes in fixed-size chunks for StreamingResponse."""
    mv = memoryview(data)
    for offset in range(0, len(mv), chunk_size):
        yield bytes(mv[offset : offset + chunk_size])


async def build_selected_docx(
    session: AsyncSession,
    *,
    title: str | None,
    raw_items: Sequence[dict[str, Any]],
    style: str = "nsfc_standard",
    numbering: str = "bracket",
    group_by_entity: bool = True,
    include_person_bio: bool = False,
    include_support_note: bool = True,
    include_relationship_note: bool | None = None,
    include_signatures_annex: bool = False,
) -> tuple[bytes, int, list[dict[str, str]]]:
    """Generate a `.docx` document for selected reuse basket items (Spec v2.2 §6.3).

    Returns `(docx_bytes, warnings_count, embedded_signatures)`.
    """
    batch_res = await fetch_and_format_batch_items(
        session,
        raw_items=raw_items,
        style=style,
        numbering=numbering,
        group_by_entity=group_by_entity,
        include_person_bio=include_person_bio,
        include_support_note=include_support_note,
        include_relationship_note=include_relationship_note,
    )
    internal_items: list[dict[str, Any]] = batch_res["_internal_items"]  # type: ignore[assignment]
    sections: list[dict[str, Any]] = batch_res["_sections"]  # type: ignore[assignment]

    scrubbed_title, _ = scrub_free_text(sanitize_xml_text(title))
    doc_title = scrubbed_title.strip() or "课题申报素材汇编"
    shanghai_today = datetime.now(UTC).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    doc = docx.Document()
    doc.add_heading(doc_title, level=1)
    doc.add_paragraph(f"生成日期：{shanghai_today}（Asia/Shanghai）｜共 {len(internal_items)} 条素材")

    for sec in sections:
        heading = sec.get("heading")
        if heading:
            doc.add_heading(sanitize_xml_text(str(heading)), level=2)
        for line in sec.get("lines") or []:
            doc.add_paragraph(sanitize_xml_text(str(line)))

    warnings_count = 0
    embedded_signatures: list[dict[str, str]] = []
    selected_person_ids: list[uuid.UUID] = []
    seen_p_ids: set[uuid.UUID] = set()
    for it in internal_items:
        if it.get("canonical_entity") == "person":
            p_obj: LabPerson = it["_obj"]
            if p_obj.id not in seen_p_ids:
                seen_p_ids.add(p_obj.id)
                selected_person_ids.append(p_obj.id)

    if include_signatures_annex and selected_person_ids:
        doc.add_heading("附：成员签名", level=2)
        rows = await session.scalars(
            select(LabPerson)
            .options(undefer(LabPerson.signature_data))
            .where(LabPerson.id.in_(selected_person_ids))
        )
        people_with_sig = {r.id: r for r in rows}

        table = doc.add_table(rows=1, cols=4)
        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = "序号"
        hdr_cells[1].text = "姓名"
        hdr_cells[2].text = "角色/职称"
        hdr_cells[3].text = "电子签名"

        embedded_total = 0
        for seq, pid in enumerate(selected_person_ids, start=1):
            person = people_with_sig.get(pid)
            if person is None:
                continue
            row_cells = table.add_row().cells
            row_cells[0].text = str(seq)
            row_cells[1].text = sanitize_xml_text(person.name)
            role_label = "负责人" if person.project_role == "applicant" else "成员"
            role_title = " / ".join(filter(None, [role_label, person.title]))
            row_cells[2].text = sanitize_xml_text(role_title)

            sig_bytes = person.signature_data
            if sig_bytes is None:
                row_cells[3].text = "（未录入）"
                continue

            check = validate_signature_image(sig_bytes)
            if not check.ok or check.mime is None:
                row_cells[3].text = "（签名数据异常，已跳过）"
                warnings_count += 1
                continue

            if check.mime == "image/webp":
                row_cells[3].text = "（WebP 签名请通过签名包导出）"
                warnings_count += 1
                continue

            if embedded_total + check.size > MAX_DOCX_SIG_BYTES:
                row_cells[3].text = "（超出单次导出图片配额，已略过）"
                warnings_count += 1
                continue

            try:
                para = row_cells[3].paragraphs[0]
                run = para.add_run()
                max_w_in = 1.5
                max_h_in = 0.45
                if (
                    check.width
                    and check.height
                    and check.width > 0
                    and check.height > 0
                ):
                    scale = min(
                        max_w_in / float(check.width),
                        max_h_in / float(check.height),
                    )
                    run.add_picture(
                        io.BytesIO(sig_bytes),
                        width=Inches(float(check.width) * scale),
                        height=Inches(float(check.height) * scale),
                    )
                else:
                    run.add_picture(io.BytesIO(sig_bytes), height=Inches(max_h_in))
                embedded_total += check.size
                embedded_signatures.append(
                    {
                        "person_id": str(person.id),
                        "sha256": check.sha256 or (person.signature_sha256 or ""),
                    }
                )
            except Exception:
                row_cells[3].text = "（签名数据异常，已跳过）"
                warnings_count += 1

    doc_text_parts = [p.text for p in doc.paragraphs]
    for tbl in doc.tables:
        for row in tbl.rows:
            for cell in row.cells:
                doc_text_parts.append(cell.text)
    try:
        assert_no_pii("\n".join(doc_text_parts))
    except PIILeakError as exc:
        raise BasketError(
            422,
            "PII_DETECTED",
            "导出内容包含未脱敏敏感信息（身份证号或电话号码），已阻断导出",
        ) from exc

    out_buf = io.BytesIO()
    doc.save(out_buf)
    return out_buf.getvalue(), warnings_count, embedded_signatures
