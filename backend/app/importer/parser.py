"""Proposal .docx parser (Table 1 cover, Table 2 team, Table 4 foundation, Table 3/6 facilities)."""

from __future__ import annotations

import copy
import hashlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.importer.fingerprint import (
    achievement_fp,
    facility_fp,
    is_redacted_placeholder,
    norm,
    project_fp,
)
from app.importer.people import assign_people_identities, normalize_person_name
from app.importer.pii import assert_no_pii, is_pii_id_or_phone, scrub_free_text
from app.importer.reader import (
    DocxValidationError,
    ParsedLegacyDoc,
    extract_body_blocks_and_images,
    extract_table_grid,
    iter_document_tables,
    normalize_label,
    validate_and_load_docx,
)
from app.importer.signature import (
    bind_proposal_signatures,
)
from app.models import LabPerson, User

PARSER_VERSION = "v2.1-hybrid"

CJK_IDEOGRAPH_CLASS = r"㐀-䶿一-鿿豈-﫿\U00020000-\U000323af"
CN_NAME_RE = re.compile(
    rf"^(?:[{CJK_IDEOGRAPH_CLASS}]{{2,20}}(?:·[{CJK_IDEOGRAPH_CLASS}]{{1,20}}){{0,3}}"
    rf"|[{CJK_IDEOGRAPH_CLASS}]{{1,20}}(?:·[{CJK_IDEOGRAPH_CLASS}]{{1,20}}){{1,3}}"
    r"|[^\W\d_][^\W\d_ .'-]{0,62}[^\W\d_.]\.?)$"
)
NON_PERSON_NAME_LABELS: frozenset[str] = frozenset(
    {
        "姓名",
        "成员姓名",
        "项目组成员",
        "主要参加者",
        "主要参与者",
        "申请人",
        "项目申请人",
        "课题负责人",
        "项目负责人",
        "负责人",
        "无",
        "空",
        "略",
        "合计",
        "签字",
        "盖章",
    }
)
SKIP_ROW_KEYWORDS = ("合计", "签字", "盖章", "以下空白", "申请资助经费", "预计完成时间")

ANCHOR_RE = re.compile(
    r"(?:^|\n)\s*(?:([1-4])\s*[．.、]\s*)?[\[［【]\s*"
    r"(学术简历|前期成果|承担项目|与(?:已承担|本)项目(?:或博士论文)?的?关系)\s*[\]］】]",
)

ITEM_SPLIT_RE = re.compile(
    r"(?:^|\n)\s*(?:[（(]\s*\d{1,3}\s*[)）]|\[\s*\d{1,3}\s*\]|\d{1,3}\s*[．.、])\s*"
)

GRANT_NO_AFTER_TITLE_RE = re.compile(
    rf"^[（(]\s*([A-Za-z0-9{CJK_IDEOGRAPH_CLASS}][A-Za-z0-9{CJK_IDEOGRAPH_CLASS}_-]{{2,24}})\s*[)）]"
)
GRANT_NO_INLINE_RE = re.compile(
    r"(?:批准号|项目编号|编号|立项号)?[:：]?\s*"
    r"\b([A-Z]{2,8}\d{2,8}|\d{4}[A-Z]{2,6}\d{1,6}|[A-Z]{2,6}\d{4}-\d{2,4})\b"
)

CN_DIGIT_MAP = {
    "〇": "0",
    "零": "0",
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}


def parse_date_string(raw: str | None) -> date | None:
    if not raw:
        return None
    text = raw.strip()
    for cn, digit in CN_DIGIT_MAP.items():
        text = text.replace(cn, digit)

    # YYYY年MM月DD日 or YYYY-MM-DD or YYYY.MM.DD or YYYY/MM/DD
    m_full = re.search(
        r"((?:19|20)\d{2})\s*(?:年|[-./])\s*(\d{1,2})\s*(?:月|[-./])\s*(\d{1,2})\s*日?",
        text,
    )
    if m_full:
        try:
            d = date(int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3)))
            if date(1990, 1, 1) <= d <= date(2035, 12, 31):
                return d
        except ValueError:
            pass

    # YYYY年MM月 or YYYY-MM or YYYY.MM
    m_ym = re.search(r"((?:19|20)\d{2})\s*(?:年|[-./])\s*(\d{1,2})\s*月?", text)
    if m_ym:
        try:
            d = date(int(m_ym.group(1)), int(m_ym.group(2)), 1)
            if date(1990, 1, 1) <= d <= date(2035, 12, 31):
                return d
        except ValueError:
            pass

    # YYYY年立项 / YYYY年
    m_y = re.search(r"((?:19|20)\d{2})\s*年", text)
    if m_y:
        try:
            d = date(int(m_y.group(1)), 1, 1)
            if date(1990, 1, 1) <= d <= date(2035, 12, 31):
                return d
        except ValueError:
            pass
    return None


def parse_proposal_docx(
    filename: str,
    raw_bytes: bytes,
    *,
    existing_users: list[User] | None = None,
    existing_people: list[LabPerson] | None = None,
    as_of_time_override: date | None = None,
    max_file_mb: int = 20,
    max_uncompressed_mb: int = 50,
    extraction_mode: str = "rule+regex_fsm",
) -> dict[str, object]:
    """Parse a grant proposal `.docx` into a sanitized preview & stats payload."""
    doc = validate_and_load_docx(
        filename,
        raw_bytes,
        max_file_mb=max_file_mb,
        max_uncompressed_mb=max_uncompressed_mb,
    )
    sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
    grids = [extract_table_grid(tbl) for tbl in iter_document_tables(doc)]

    pii_dropped = 0
    warnings: list[dict[str, object]] = []

    cover, cover_pii = _extract_cover(grids)
    pii_dropped += cover_pii
    as_of_time_str = str(cover.get("as_of_time") or "") or None
    as_of_date = as_of_time_override or (
        date.fromisoformat(as_of_time_str) if as_of_time_str else None
    )
    if as_of_date is None:
        warnings.append({"code": "AS_OF_TIME_MISSING"})

    people, team_pii = _extract_people(
        grids,
        default_affiliation=str(cover.get("affiliation") or "") or None,
    )
    pii_dropped += team_pii
    if not people:
        raise DocxValidationError(
            422,
            "import_parse_failed",
            "未能从申报书中识别出申请人或项目组成员表格",
        )

    bio, achievements, projects, relationship_note, t4_pii, t4_warnings = _extract_t4_foundation(
        grids,
        as_of_date=as_of_date,
    )
    pii_dropped += t4_pii
    warnings.extend(t4_warnings)

    if bio:
        for person in people:
            if person.get("project_role") == "applicant":
                person["bio"] = bio
                break

    people_snapshot = copy.deepcopy(people)

    assign_people_identities(
        people,
        existing_users=existing_users,
        existing_people=existing_people,
    )

    if isinstance(doc, ParsedLegacyDoc):
        body_blocks, embedded_images = doc.body_blocks, doc.embedded_images
    else:
        body_blocks, embedded_images = extract_body_blocks_and_images(
            raw_bytes,
            max_uncompressed_mb=max_uncompressed_mb,
        )
    signatures, sig_blobs, sig_warnings = bind_proposal_signatures(
        people=people,
        grids=grids,
        embedded_images=embedded_images,
        body_blocks=body_blocks,
    )
    warnings.extend(sig_warnings)

    facilities, fac_pii = _extract_facilities(grids)
    pii_dropped += fac_pii

    if pii_dropped > 0:
        warnings.append({"code": "PII_DROPPED", "count": pii_dropped})

    source_snapshot: dict[str, object] = {
        "cover": copy.deepcopy(cover),
        "people": people_snapshot,
        "facilities": copy.deepcopy(facilities),
        "t4": {
            "bio": bio,
            "relationship_note": relationship_note,
            "achievement_raw_items": [str(a.get("raw_text") or "") for a in achievements],
            "project_raw_items": [str(j.get("raw_text") or "") for j in projects],
        },
    }

    preview: dict[str, object] = {
        "parser_version": PARSER_VERSION,
        "extraction_mode": extraction_mode,
        "as_of_time": as_of_date.isoformat() if as_of_date else None,
        "cover": cover,
        "relationship_note": relationship_note,
        "people": people,
        "signatures": signatures,
        "_signature_blobs": sig_blobs,
        "achievements": achievements,
        "projects": projects,
        "facilities": facilities,
        "warnings": warnings,
        "source_snapshot": source_snapshot,
    }

    assert_no_pii(preview)

    stats: dict[str, object] = {
        "people": len(people),
        "signatures": len(signatures),
        "achievements": len(achievements),
        "projects": len(projects),
        "facilities": len(facilities),
        "pii_dropped": pii_dropped,
    }
    return {
        "content_sha256": sha256_hex,
        "parser_version": PARSER_VERSION,
        "as_of_time": as_of_date,
        "preview": preview,
        "stats": stats,
    }


def _scrub_reparse_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return scrub_free_text(obj)[0]
    if isinstance(obj, dict):
        return {
            k: (v if str(k).startswith("_") else _scrub_reparse_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_reparse_obj(it) for it in obj]
    return obj


def reparse_preview_payload(
    preview: dict[str, Any],
    *,
    as_of_date: date | None,
    mode: str,
    existing_users: list[User] | None = None,
    existing_people: list[LabPerson] | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Re-run extraction on `source_snapshot` (or fallback `raw_text`) and refresh identities."""
    preview = _scrub_reparse_obj(preview)
    snapshot = preview.get("source_snapshot")
    if isinstance(snapshot, dict):
        raw_cover = snapshot.get("cover")
        if isinstance(raw_cover, dict):
            preview["cover"] = copy.deepcopy(raw_cover)
        raw_people = snapshot.get("people")
        people_list: list[dict[str, object]] = [
            copy.deepcopy(p)
            for p in (raw_people if isinstance(raw_people, list) else [])
            if isinstance(p, dict)
        ]
        raw_facs = snapshot.get("facilities")
        facilities_list: list[dict[str, object]] = [
            copy.deepcopy(f)
            for f in (raw_facs if isinstance(raw_facs, list) else [])
            if isinstance(f, dict)
        ]
        t4 = snapshot.get("t4")
        t4_dict = t4 if isinstance(t4, dict) else {}
        rel_note = (
            str(t4_dict.get("relationship_note") or "").strip() or None
            if t4_dict.get("relationship_note")
            else None
        )
        preview["relationship_note"] = rel_note
        ach_raw_items = [
            str(x)
            for x in (t4_dict.get("achievement_raw_items") or [])
            if str(x).strip()
        ]
        proj_raw_items = [
            str(x)
            for x in (t4_dict.get("project_raw_items") or [])
            if str(x).strip()
        ]
        achievements_list = [
            _parse_achievement_item(raw_item, index=idx)
            for idx, raw_item in enumerate(ach_raw_items, start=1)
        ]
        projects_list = [
            _parse_project_item(
                raw_item,
                index=idx,
                as_of_date=as_of_date,
                overall_relationship=rel_note,
            )
            for idx, raw_item in enumerate(proj_raw_items, start=1)
        ]
    else:
        raw_people = preview.get("people")
        people_list = [
            copy.deepcopy(p)
            for p in (raw_people if isinstance(raw_people, list) else [])
            if isinstance(p, dict)
        ]
        for p in people_list:
            if p.get("tier") == "T1" and not p.get("cert"):
                p["cert"] = p.get("student_id") or p.get("login_name")
        raw_facs = preview.get("facilities")
        facilities_list = [
            copy.deepcopy(f)
            for f in (raw_facs if isinstance(raw_facs, list) else [])
            if isinstance(f, dict)
        ]
        rel_note = (
            str(preview.get("relationship_note") or "").strip() or None
            if preview.get("relationship_note")
            else None
        )
        raw_achs = preview.get("achievements")
        achievements_list = [
            _parse_achievement_item(
                str(a.get("raw_text") or a.get("title") or ""),
                index=idx,
            )
            for idx, a in enumerate(
                (raw_achs if isinstance(raw_achs, list) else []), start=1
            )
            if isinstance(a, dict) and str(a.get("raw_text") or a.get("title") or "").strip()
        ]
        raw_projs = preview.get("projects")
        projects_list = [
            _parse_project_item(
                str(j.get("raw_text") or j.get("title") or ""),
                index=idx,
                as_of_date=as_of_date,
                overall_relationship=rel_note,
            )
            for idx, j in enumerate(
                (raw_projs if isinstance(raw_projs, list) else []), start=1
            )
            if isinstance(j, dict) and str(j.get("raw_text") or j.get("title") or "").strip()
        ]

    assign_people_identities(
        people_list,
        existing_users=existing_users,
        existing_people=existing_people,
    )
    parser_ver = "v2.1-llm" if mode == "llm" else "v2.1-fsm"
    ext_mode = "rule+llm" if mode == "llm" else "rule+regex_fsm"
    preview["people"] = people_list
    preview["achievements"] = achievements_list
    preview["projects"] = projects_list
    preview["facilities"] = facilities_list
    preview["parser_version"] = parser_ver
    preview["extraction_mode"] = ext_mode
    if as_of_date is not None:
        preview["as_of_time"] = as_of_date.isoformat()

    assert_no_pii(preview)
    recomputed_stats = {
        "people": len(people_list),
        "projects": len(projects_list),
        "achievements": len(achievements_list),
        "facilities": len(facilities_list),
        "people_parsed": len(people_list),
        "projects_parsed": len(projects_list),
        "achievements_parsed": len(achievements_list),
        "facilities_parsed": len(facilities_list),
    }
    return preview, recomputed_stats


def _extract_cover(grids: list[list[list[str]]]) -> tuple[dict[str, object], int]:
    cover: dict[str, object] = {
        "project_title": None,
        "category": None,
        "discipline": None,
        "applicant_name": None,
        "affiliation": None,
        "as_of_time": None,
    }
    pii_count = 0
    for grid in grids[:3]:
        flat_labels = " ".join(normalize_label(cell) for row in grid for cell in row)
        if not any(k in flat_labels for k in ("项目名称", "课题名称", "填表日期", "项目申请人", "课题负责人")):
            continue
        for row in grid:
            i = 0
            while i < len(row):
                label = normalize_label(row[i])
                val = row[i + 1].strip() if i + 1 < len(row) else ""
                if is_pii_id_or_phone(val):
                    pii_count += 1
                    i += 2
                    continue
                if label in ("项目名称", "课题名称") and val and not cover["project_title"]:
                    cover["project_title"] = val
                    i += 2
                elif label in ("项目类别", "课题类别") and val and not cover["category"]:
                    cover["category"] = val
                    i += 2
                elif label in ("学科分类",) and val and not cover["discipline"]:
                    cover["discipline"] = val
                    i += 2
                elif (
                    label in ("项目申请人", "课题负责人", "申请人", "负责人")
                    and val
                    and not cover["applicant_name"]
                ):
                    cover["applicant_name"] = normalize_person_name(val)
                    i += 2
                elif (
                    label in ("责任单位", "负责人所在单位", "所在单位", "申请单位")
                    and val
                    and not cover["affiliation"]
                ):
                    cover["affiliation"] = val
                    i += 2
                elif (
                    label in ("填表日期", "填报日期", "申请日期", "日期")
                    and val
                    and not cover["as_of_time"]
                ):
                    parsed_d = parse_date_string(val)
                    if parsed_d is not None:
                        cover["as_of_time"] = parsed_d.isoformat()
                    i += 2
                elif any(p in label for p in ("联系电话", "手机", "电话", "身份证")):
                    if val:
                        pii_count += 1
                    i += 2
                else:
                    i += 1
    return cover, pii_count


APPLICANT_KV_MAP: dict[str, str] = {
    "申请人姓名": "name",
    "课题负责人": "name",
    "负责人姓名": "name",
    "姓名": "name",
    "行政职务": "admin_duty",
    "专业职称": "title",
    "专业职务": "title",
    "专业技术职务": "title",
    "职称": "title",
    "研究专长": "research_specialty",
    "最后学位": "degree",
    "最终学位": "degree",
    "最高学位": "degree",
    "学位": "degree",
    "担任导师": "tutor_role",
    "工作单位": "affiliation",
    "所在单位": "affiliation",
    "电子信箱": "email",
    "电子邮箱": "email",
    "邮箱": "email",
    "email": "email",
    "身份证件号码": "__PII__",
    "身份证号": "__PII__",
    "身份证号码": "__PII__",
    "证件号码": "__PII__",
    "证件号": "__PII__",
    "联系电话": "__PII__",
    "手机": "__PII__",
    "电话": "__PII__",
    "通讯地址": "__DROP__",
    "单位通讯地址": "__DROP__",
    "邮政编码": "__DROP__",
    "出生日期": "__DROP__",
    "性别": "__DROP__",
    "民族": "__DROP__",
    "身份证类型": "__DROP__",
    "最后学历": "__DROP__",
    "所在省自治区直辖市": "__DROP__",
    "所属系统": "__DROP__",
    "是否在内地大陆工作的港澳台研究人员": "__DROP__",
}

MEMBER_HEADER_MAP: dict[str, str] = {
    "姓名": "name",
    "证件号": "cert",
    "证件号码": "cert",
    "身份证号": "cert",
    "学号": "cert",
    "工号": "cert",
    "学号/工号": "cert",
    "专业职务": "title",
    "专业职称": "title",
    "技术职称": "title",
    "专业技术职务": "title",
    "职称": "title",
    "职务": "title",
    "行政职务": "admin_duty",
    "担任导师": "tutor_role",
    "研究专长": "research_specialty",
    "学位": "degree",
    "最后学位": "degree",
    "最终学位": "degree",
    "最高学位": "degree",
    "学历学位": "degree",
    "工作单位": "affiliation",
    "单位": "affiliation",
    "单位名称": "affiliation",
    "所在单位": "affiliation",
    "所在单位名称": "affiliation",
    "电子信箱": "email",
    "电子邮箱": "email",
    "邮箱": "email",
    "联系电话": "__PII__",
    "手机": "__PII__",
    "电话": "__PII__",
    "出生年月": "__DROP__",
    "出生日期": "__DROP__",
    "性别": "__DROP__",
}


_FORM_HEADER_LABELS_LOWER: frozenset[str] = (
    frozenset(lbl.lower() for lbl in NON_PERSON_NAME_LABELS)
    | frozenset(APPLICANT_KV_MAP.keys())
    | frozenset(normalize_label(k).lower() for k in MEMBER_HEADER_MAP)
)


def _is_plausible_person_name(raw_name: str) -> bool:
    name = normalize_person_name(raw_name)
    if not (2 <= len(name) <= 64):
        return False
    lbl = normalize_label(name).lower()
    if (
        name in NON_PERSON_NAME_LABELS
        or lbl in _FORM_HEADER_LABELS_LOWER
        or any(kw in name for kw in SKIP_ROW_KEYWORDS)
    ):
        return False
    if any(ch.isdigit() for ch in name):
        return False
    if not name[0].isalpha() or not (name[-1].isalpha() or name[-1] == "."):
        return False
    alpha_count = 0
    for ch in name:
        if ch.isalpha():
            alpha_count += 1
        elif ch not in " ·.'-":
            return False
    return alpha_count >= 2


def _is_member_header_row(row: list[str]) -> bool:
    labels = [normalize_label(c) for c in row if c.strip()]
    if any(lbl in ("男", "女") for lbl in labels):
        return False
    has_name = "姓名" in labels
    matched_cols = sum(1 for lbl in labels if lbl in MEMBER_HEADER_MAP)
    return has_name and matched_cols >= 4


def _extract_people(
    grids: list[list[list[str]]],
    *,
    default_affiliation: str | None = None,
) -> tuple[list[dict[str, object]], int]:
    pii_count = 0
    people: list[dict[str, object]] = []

    for tbl_idx, grid in enumerate(grids):
        header_row_idx: int | None = None
        for r_idx, row in enumerate(grid):
            if _is_member_header_row(row):
                header_row_idx = r_idx
                break

        has_applicant_kv = any(
            normalize_label(cell) in ("申请人姓名", "行政职务", "专业职称", "身份证件号码")
            for row in grid
            for cell in row
        )
        if header_row_idx is None and not has_applicant_kv:
            continue

        # 1) Parse Applicant KV rows before header_row_idx
        kv_end = header_row_idx if header_row_idx is not None else len(grid)
        applicant_row_idx = 0
        applicant: dict[str, object] = {
            "name": "",
            "title": None,
            "admin_duty": None,
            "tutor_role": None,
            "research_specialty": None,
            "degree": None,
            "affiliation": default_affiliation,
            "email": None,
            "cert": None,
            "project_role": "applicant",
            "bio": None,
            "warnings": [],
        }
        applicant_warnings: list[str] = []
        for r_idx, row in enumerate(grid[:kv_end]):
            i = 0
            while i < len(row):
                lbl = normalize_label(row[i]).lower()
                target = APPLICANT_KV_MAP.get(lbl)
                if target is not None and i + 1 < len(row):
                    val = row[i + 1].strip()
                    if target == "__PII__":
                        if val:
                            pii_count += 1
                            if "身份证" in lbl and "CERT_DROPPED" not in applicant_warnings:
                                applicant_warnings.append("CERT_DROPPED")
                        i += 2
                        continue
                    if target == "__DROP__":
                        if is_pii_id_or_phone(val):
                            pii_count += 1
                        i += 2
                        continue
                    if is_pii_id_or_phone(val):
                        pii_count += 1
                        i += 2
                        continue
                    if val:
                        if target == "name":
                            applicant["name"] = normalize_person_name(val)
                            applicant_row_idx = r_idx
                        else:
                            applicant[target] = val
                    i += 2
                else:
                    if is_pii_id_or_phone(row[i]):
                        pii_count += 1
                    i += 1

        applicant["warnings"] = applicant_warnings
        applicant["source"] = {"table_index": tbl_idx, "row_index": applicant_row_idx}
        app_name = str(applicant.get("name") or "")
        if _is_plausible_person_name(app_name):
            people.append(applicant)

        # 2) Parse Member table rows starting at header_row_idx + 1
        if header_row_idx is not None:
            header_row = grid[header_row_idx]
            col_map: dict[int, str] = {}
            for c_idx, cell_text in enumerate(header_row):
                lbl = normalize_label(cell_text)
                if lbl in MEMBER_HEADER_MAP:
                    col_map[c_idx] = MEMBER_HEADER_MAP[lbl]

            for r_idx in range(header_row_idx + 1, len(grid)):
                row = grid[r_idx]
                joined_row = " ".join(row)
                if any(kw in joined_row for kw in SKIP_ROW_KEYWORDS):
                    continue
                member: dict[str, object] = {
                    "name": "",
                    "title": None,
                    "admin_duty": None,
                    "tutor_role": None,
                    "research_specialty": None,
                    "degree": None,
                    "affiliation": default_affiliation,
                    "email": None,
                    "cert": None,
                    "project_role": "member",
                    "bio": None,
                    "warnings": [],
                    "source": {"table_index": tbl_idx, "row_index": r_idx},
                    "member_source": {
                        "table_index": tbl_idx,
                        "row_index": r_idx,
                        "header_row_index": header_row_idx,
                    },
                }
                mem_warnings: list[str] = []
                # Handle possible shift if row has fewer cells than header_row due to missing leading merged group cell
                offset = 0
                if (
                    len(row) + 1 == len(header_row)
                    and 0 not in col_map
                    and normalize_label(header_row[0]) in ("项目组成员", "主要参加者", "主要参与者")
                ):
                    offset = -1

                for c_idx, target in col_map.items():
                    actual_idx = c_idx + offset
                    if actual_idx < 0 or actual_idx >= len(row):
                        continue
                    val = row[actual_idx].strip()
                    if not val:
                        continue
                    if target == "__PII__":
                        pii_count += 1
                        continue
                    if target == "__DROP__":
                        if is_pii_id_or_phone(val):
                            pii_count += 1
                        continue
                    if target == "cert":
                        if is_pii_id_or_phone(val):
                            pii_count += 1
                            mem_warnings.append("CERT_DROPPED")
                        else:
                            member["cert"] = val
                        continue
                    if is_pii_id_or_phone(val):
                        pii_count += 1
                        continue
                    if target == "name":
                        member["name"] = normalize_person_name(val)
                    else:
                        member[target] = val

                raw_name = str(member.get("name") or "")
                if not _is_plausible_person_name(raw_name):
                    continue
                member["warnings"] = mem_warnings
                people.append(member)

    return _dedup_people(people), pii_count


def _people_identifiers_conflict(
    a: dict[str, object], b: dict[str, object]
) -> bool:
    c_a = norm(
        str(a.get("cert") or a.get("student_or_staff_id") or a.get("student_id") or "")
    )
    c_b = norm(
        str(b.get("cert") or b.get("student_or_staff_id") or b.get("student_id") or "")
    )
    if c_a and c_b and c_a != c_b:
        return True
    e_a = str(a.get("email") or "").strip().lower()
    e_b = str(b.get("email") or "").strip().lower()
    if e_a and e_b and e_a != e_b:
        return True
    by_a = str(a.get("birth_year") or "").strip()
    by_b = str(b.get("birth_year") or "").strip()
    if by_a and by_b and by_a != by_b:
        return True
    return False


def _dedup_people(people: list[dict[str, object]]) -> list[dict[str, object]]:
    """Deduplicate by (normalized name, normalized affiliation), preserving distinct identifiers."""
    deduped: list[dict[str, object]] = []
    by_key: dict[tuple[str, str], list[dict[str, object]]] = {}
    for person in people:
        pname = str(person.get("name") or "")
        paff = norm(str(person.get("affiliation") or ""))
        key = (norm(pname), paff)
        matched_existing: dict[str, object] | None = None
        for cand in by_key.get(key, []):
            if not _people_identifiers_conflict(cand, person):
                matched_existing = cand
                break
        if matched_existing is not None:
            for field in (
                "title",
                "admin_duty",
                "tutor_role",
                "research_specialty",
                "degree",
                "affiliation",
                "email",
                "cert",
                "student_or_staff_id",
                "student_id",
                "birth_year",
                "bio",
                "source",
                "member_source",
            ):
                if not matched_existing.get(field) and person.get(field):
                    matched_existing[field] = person[field]
            if person.get("project_role") == "applicant":
                matched_existing["project_role"] = "applicant"
        else:
            by_key.setdefault(key, []).append(person)
            deduped.append(person)
    return deduped


def _extract_t4_foundation(
    grids: list[list[list[str]]],
    *,
    as_of_date: date | None,
) -> tuple[
    str | None,
    list[dict[str, object]],
    list[dict[str, object]],
    str | None,
    int,
    list[dict[str, object]],
]:
    pii_total = 0
    warnings: list[dict[str, object]] = []
    foundation_markers = (
        "[学术简历]",
        "[前期成果]",
        "[承担项目]",
        "[与本项目关系]",
        "［学术简历］",
        "［前期成果］",
        "［承担项目］",
        "［与本项目关系］",
    )

    def _parse_foundation_sections(cand: str) -> dict[str, str]:
        sec_map: dict[str, str] = {}
        matches = list(ANCHOR_RE.finditer(cand))
        for idx, match in enumerate(matches):
            sec_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(cand)
            raw_title = match.group(2)
            body = cand[match.end() : sec_end].strip()
            if "学术简历" in raw_title:
                sec_map["resume"] = body
            elif "前期成果" in raw_title:
                sec_map["achievements"] = body
            elif "承担项目" == raw_title:
                sec_map["projects"] = body
            elif "关系" in raw_title:
                sec_map["relationship"] = body
        return sec_map

    candidates: list[tuple[str, int]] = []
    for grid in grids:
        grid_cells: list[str] = []
        for row in grid:
            for cell in row:
                if cell:
                    has_marker = any(k in cell for k in foundation_markers)
                    if has_marker or not any(
                        kw in cell for kw in (*SKIP_ROW_KEYWORDS, "填写说明", "填表说明")
                    ):
                        grid_cells.append(cell)
                    if has_marker:
                        candidates.append((cell, 1))
        if len(grid_cells) > 1 and any(
            any(k in c for k in foundation_markers) for c in grid_cells
        ):
            candidates.append(("\n".join(grid_cells), 0))

    if not candidates:
        return None, [], [], None, 0, [{"code": "SECTION_NOT_FOUND", "section": "foundation"}]

    sections: dict[str, str] = {}
    best_score: tuple[int, int, int, int] = (-1, -1, -1, -1)
    for cand, is_single_cell in candidates:
        sec_map = _parse_foundation_sections(cand)
        non_empty_cnt = sum(1 for v in sec_map.values() if v.strip())
        anchor_cnt = len(sec_map)
        body_chars = sum(len(v.strip()) for v in sec_map.values())
        score = (non_empty_cnt, anchor_cnt, is_single_cell, body_chars)
        if score > best_score:
            best_score = score
            sections = sec_map

    bio: str | None = None
    if "resume" in sections and sections["resume"]:
        scrubbed_bio, cnt = scrub_free_text(sections["resume"])
        pii_total += cnt
        bio = scrubbed_bio.strip()

    relationship_note: str | None = None
    if "relationship" in sections and sections["relationship"]:
        scrubbed_rel, cnt = scrub_free_text(sections["relationship"])
        pii_total += cnt
        relationship_note = scrubbed_rel.strip()

    achievements: list[dict[str, object]] = []
    if "achievements" in sections and sections["achievements"]:
        raw_items = [
            it.strip()
            for it in ITEM_SPLIT_RE.split(sections["achievements"])
            if it and it.strip()
        ]
        for a_idx, raw_item in enumerate(raw_items, start=1):
            scrubbed_item, cnt = scrub_free_text(raw_item)
            pii_total += cnt
            parsed_ach = _parse_achievement_item(scrubbed_item, index=a_idx)
            achievements.append(parsed_ach)

    projects: list[dict[str, object]] = []
    if "projects" in sections and sections["projects"]:
        raw_items = [
            it.strip()
            for it in ITEM_SPLIT_RE.split(sections["projects"])
            if it and it.strip()
        ]
        for p_idx, raw_item in enumerate(raw_items, start=1):
            scrubbed_item, cnt = scrub_free_text(raw_item)
            pii_total += cnt
            parsed_proj = _parse_project_item(
                scrubbed_item,
                index=p_idx,
                as_of_date=as_of_date,
                overall_relationship=relationship_note,
            )
            projects.append(parsed_proj)

    return bio, achievements, projects, relationship_note, pii_total, warnings


def _detect_achievement_kind(text: str) -> str:
    if any(k in text for k in ("研究报告", "资政报告", "咨询报告", "决策参考")):
        return "report"
    if any(k in text for k in ("成果展", "动态展", "展览", "参展")):
        return "exhibition"
    if any(k in text for k in ("专利", "ZL", "发明")):
        return "patent"
    if any(k in text for k in ("软件著作权", "软著", "登记号")):
        return "software"
    if any(k in text for k in ("一等奖", "二等奖", "三等奖", "获奖", "奖励")):
        return "award"
    if any(k in text for k in ("专著", "编著", "教材", "出版社")):
        return "book"
    if any(k in text for k in ("论文", "期刊", "学报", "[J]", "[C]", "探索科学", "时代教育")):
        return "paper"
    return "other"


def _parse_achievement_item(raw_item: str, *, index: int) -> dict[str, object]:
    text = re.sub(r"\s+", " ", raw_item).strip()
    first_sentence, _, rest_sentences = text.partition("。")
    support_note = rest_sentences.strip() or None

    first_author: str | None = None
    authors_text: str | None = None
    title = ""
    venue: str | None = None
    year: int | None = None
    parse_quality = "structured"
    item_warnings: list[str] = []

    m_lead = re.match(
        rf"^([{CJK_IDEOGRAPH_CLASS}·]{{2,10}})\s*(?:[（(]第一[)）])?\s*[:：]\s*(.+)$",
        first_sentence,
    )
    if m_lead:
        first_author = normalize_person_name(m_lead.group(1))[:64]
        authors_text = first_author
        remainder = m_lead.group(2).strip()
        parts = [p.strip() for p in re.split(r"[，,]", remainder) if p.strip()]
        if parts:
            raw_title = parts[0]
            if raw_title.startswith("《") and raw_title.endswith("》"):
                title = raw_title[1:-1].strip()
            else:
                title = raw_title
        kind = _detect_achievement_kind(first_sentence)
        explicit_kind_re = re.compile(
            r"^(学术论文|期刊论文|会议论文|论文|专著|著作|教材|编著|译著|"
            r"发明专利|专利|软件著作权|软著|研究报告|咨询报告|报告|标准|奖项|获奖)$"
        )
        bare_year_re = re.compile(r"^(?:19|20)\d{2}\s*年?(?:\d{1,2}月?)?$")
        raw_venue = ""
        if len(parts) >= 2:
            if explicit_kind_re.match(parts[1]):
                raw_venue = parts[2] if len(parts) >= 3 else ""
            else:
                raw_venue = parts[1]
        if raw_venue.startswith("《") and raw_venue.endswith("》"):
            raw_venue = raw_venue[1:-1].strip()
        if raw_venue and not bare_year_re.match(raw_venue):
            venue = raw_venue
        years = re.findall(r"((?:19|20)\d{2})", first_sentence)
        if years:
            year = int(years[-1])
    else:
        kind = _detect_achievement_kind(text)
        m_title = re.search(r"《(.+?)》", first_sentence)
        if m_title:
            title = m_title.group(1).strip()
        else:
            title = first_sentence[:200]
            parse_quality = "raw"
            item_warnings.append("ACHIEVEMENT_RAW")
        m_author = re.match(rf"^([{CJK_IDEOGRAPH_CLASS}·]{{2,6}})", first_sentence)
        if m_author:
            first_author = normalize_person_name(m_author.group(1))[:64]
            authors_text = first_author
        years = re.findall(r"((?:19|20)\d{2})", first_sentence)
        if years:
            year = int(years[-1])

    fp_val = achievement_fp(
        kind=kind,
        title=title,
        year=year,
        raw_text=text,
        parse_quality=parse_quality,
    )
    return {
        "key": f"a{index}",
        "kind": kind,
        "title": title,
        "first_author": first_author,
        "authors_text": authors_text,
        "venue": venue,
        "year": year,
        "support_note": support_note,
        "raw_text": text,
        "parse_quality": parse_quality,
        "fingerprint": fp_val,
        "warnings": item_warnings,
    }


def _parse_project_item(
    raw_item: str,
    *,
    index: int,
    as_of_date: date | None,
    overall_relationship: str | None,
) -> dict[str, object]:
    text = re.sub(r"\s+", " ", raw_item).strip()
    item_warnings: list[str] = []
    if "[已脱敏]" in text:
        item_warnings.append("GRANT_NO_REDACTED")

    title = ""
    grant_no: str | None = None
    after_title = text

    m_title = re.search(r"《(.+?)》", text)
    if m_title:
        title = m_title.group(1).strip()
        after_title = text[m_title.end() :].strip()
        m_grant = GRANT_NO_AFTER_TITLE_RE.match(after_title)
        if m_grant:
            grant_no = m_grant.group(1).strip()
            after_title = after_title[m_grant.end() :].strip()
    else:
        title = text[:200]

    if grant_no is None:
        m_inline = GRANT_NO_INLINE_RE.search(text)
        if m_inline:
            grant_no = m_inline.group(1).strip()
    if grant_no and is_redacted_placeholder(grant_no):
        grant_no = None
        if "GRANT_NO_REDACTED" not in item_warnings:
            item_warnings.append("GRANT_NO_REDACTED")
    elif grant_no:
        grant_no = grant_no[:64]

    after_title = after_title.lstrip("，,。；; ")
    segments = [
        seg.strip() for seg in re.split(r"[，,。；;]", after_title) if seg and seg.strip()
    ]

    pi_name: str | None = None
    lab_role = "unknown"
    funder: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    amount_wan: str | None = None

    for seg in segments:
        if pi_name is None:
            m_host = re.match(
                rf"^(?:主持人|负责人)?[:：]?\s*([{CJK_IDEOGRAPH_CLASS}·]{{2,6}})\s*(?:[（(]?主持[)）]?|负责人)$",
                seg,
            )
            if m_host:
                pi_name = normalize_person_name(m_host.group(1))[:64]
                lab_role = "host"
                continue
            m_part = re.match(
                rf"^([{CJK_IDEOGRAPH_CLASS}·]{{2,6}})\s*[（(]?(?:参与|参加)[)）]?$",
                seg,
            )
            if m_part:
                pi_name = normalize_person_name(m_part.group(1))[:64]
                lab_role = "participant"
                continue

        if end_date is None and ("立项" in seg or re.search(r"(?:19|20)\d{2}", seg)):
            m_range = re.search(
                r"((?:19|20)\d{2})(?:[年./-](\d{1,2}))?\s*[-—–~至到]\s*((?:19|20)\d{2})(?:[年./-](\d{1,2}))?",
                seg,
            )
            if m_range:
                s_y = int(m_range.group(1))
                s_m = int(m_range.group(2)) if m_range.group(2) else 1
                e_y = int(m_range.group(3))
                e_m = int(m_range.group(4)) if m_range.group(4) else 12
                try:
                    start_date = date(s_y, s_m, 1)
                    end_date = date(e_y, e_m, 1)
                except ValueError:
                    pass
                continue
            if start_date is None:
                parsed_start = parse_date_string(seg)
                if parsed_start is not None:
                    start_date = parsed_start
                    continue

        m_amt = re.search(r"(\d+(?:\.\d+)?)\s*万元?", seg)
        if m_amt and amount_wan is None:
            try:
                amount_wan = f"{Decimal(m_amt.group(1)):.2f}"
            except InvalidOperation:
                pass
            continue

        if (
            funder is None
            and not any(
                k in seg
                for k in ("在研", "结题", "验收", "完成", "立项", "主持", "参与")
            )
            and len(seg) >= 2
        ):
            funder = seg[:255]

    status = "unknown"
    status_source = "inferred"
    if any(k in text for k in ("已结题", "已通过验收", "通过验收", "已完成", "已结项", "结题")):
        status = "completed"
        status_source = "explicit"
    elif any(k in text for k in ("在研", "进行中", "执行中")):
        status = "ongoing"
        status_source = "explicit"
    elif end_date is not None and as_of_date is not None:
        status = "completed" if end_date < as_of_date else "ongoing"
        status_source = "inferred"

    fp_val = project_fp(
        grant_no=grant_no,
        title=title,
        funder=funder,
        warnings=item_warnings,
    )
    return {
        "key": f"j{index}",
        "title": title,
        "funder": funder,
        "grant_no": grant_no,
        "pi_name": pi_name,
        "lab_role": lab_role,
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
        "amount_wan": amount_wan,
        "status": status,
        "status_source": status_source,
        "relationship_note": overall_relationship,
        "raw_text": text,
        "fingerprint": fp_val,
        "warnings": item_warnings,
    }


def _extract_facilities(
    grids: list[list[list[str]]],
) -> tuple[list[dict[str, object]], int]:
    facilities: list[dict[str, object]] = []
    pii_total = 0
    seen_fps: set[str] = set()

    for grid in grids:
        for row in grid:
            joined = " ".join(row)
            if not any(k in joined for k in ("设备费", "计算加速设备", "服务器", "算力", "实验环境")):
                continue
            # Check if this is a budget table row like ['直接费用', '3', '设备费', '8.00', '用于...']
            equip_indices = [
                idx for idx, c in enumerate(row) if normalize_label(c) == "设备费"
            ]
            if equip_indices:
                equip_idx = equip_indices[0]
                raw_desc = (
                    row[-1]
                    if (len(row) >= 3 and len(row) - 1 > equip_idx)
                    else joined
                )
                scrubbed_desc, cnt = scrub_free_text(raw_desc)
                pii_total += cnt
                value_wan: str | None = None
                saw_zero_amount = False
                amount_cells = (
                    row[equip_idx + 1 : -1]
                    if len(row) - 1 > equip_idx + 1
                    else row[equip_idx + 1 :]
                )
                for c in amount_cells:
                    m_amt = re.match(r"^(\d+(?:\.\d+)?)\s*(?:万元|万)?$", c.strip())
                    if m_amt:
                        try:
                            dec = Decimal(m_amt.group(1))
                            if dec > 0:
                                value_wan = f"{dec:.2f}"
                                break
                            saw_zero_amount = True
                        except InvalidOperation:
                            pass
                desc_clean = scrubbed_desc.strip()
                if (value_wan is None and saw_zero_amount) or (
                    desc_clean in ("", "无", "无。", "-", "—", "/", "0", "0.00")
                    or any(
                        neg in desc_clean
                        for neg in ("无新购", "无购置", "不购置", "不新购", "无需购置")
                    )
                ):
                    continue
                category = (
                    "compute"
                    if any(k in scrubbed_desc for k in ("计算", "算力", "推理", "微调", "GPU"))
                    else "equipment"
                )
                spec = re.split(r"[；;]", scrubbed_desc)[0].strip()
                m_name = re.search(r"所需的([^；;。，,]+)", scrubbed_desc)
                m_verb_name = (
                    None
                    if m_name
                    else re.search(
                        r"(?:购置|采购|配置|配备)([^；;。，,]{2,30})", scrubbed_desc
                    )
                )
                if m_name and m_name.group(1).strip():
                    name = m_name.group(1).strip()
                elif m_verb_name and m_verb_name.group(1).strip():
                    name = m_verb_name.group(1).strip()
                elif 2 <= len(spec) <= 30:
                    name = spec
                else:
                    name = (
                        "计算与实验支撑设备"
                        if category == "compute"
                        else "科研仪器与支撑设备"
                    )
                usage: str | None = scrubbed_desc
                m_usage = re.search(r"用于(.+?)所需的", scrubbed_desc)
                if m_usage:
                    usage = m_usage.group(1).strip()
                fp_val = facility_fp(category=category, name=name, spec=spec)
                if fp_val not in seen_fps:
                    seen_fps.add(fp_val)
                    facilities.append(
                        {
                            "key": f"f{len(facilities) + 1}",
                            "category": category,
                            "name": name,
                            "spec": spec,
                            "value_wan": value_wan,
                            "usage_scenario": usage,
                            "raw_text": scrubbed_desc,
                            "fingerprint": fp_val,
                        }
                    )

    return facilities, pii_total
