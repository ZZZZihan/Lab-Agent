"""Proposal .docx parser (Table 1 cover, Table 2 team, Table 4 foundation, Table 3/6 facilities)."""

from __future__ import annotations

import hashlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.importer.fingerprint import achievement_fp, facility_fp, norm, project_fp
from app.importer.people import assign_people_identities, normalize_person_name
from app.importer.pii import assert_no_pii, is_pii_id_or_phone, scrub_free_text
from app.importer.reader import (
    DocxValidationError,
    extract_table_grid,
    normalize_label,
    validate_and_load_docx,
)
from app.models import LabPerson, User

PARSER_VERSION = "v2.1-hybrid"

CN_NAME_RE = re.compile(r"^[一-龥·]{2,10}$")
SKIP_ROW_KEYWORDS = ("合计", "签字", "盖章", "以下空白", "申请资助经费", "预计完成时间")

ANCHOR_RE = re.compile(
    r"(?:^|\n)\s*([1-4])\s*[．.、]\s*[\[［【]\s*"
    r"(学术简历|前期成果|承担项目|与已承担项目(?:或博士论文)?的关系)\s*[\]］】]",
)

ITEM_SPLIT_RE = re.compile(
    r"(?:^|\n)\s*(?:[（(]\s*\d{1,3}\s*[)）]|\[\s*\d{1,3}\s*\]|\d{1,3}\s*[．.、])\s*"
)

GRANT_NO_AFTER_TITLE_RE = re.compile(
    r"^[（(]\s*([A-Za-z0-9一-龥][A-Za-z0-9一-龥_-]{2,24})\s*[)）]"
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
    grids = [extract_table_grid(tbl) for tbl in doc.tables]

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

    assign_people_identities(
        people,
        existing_users=existing_users,
        existing_people=existing_people,
    )

    facilities, fac_pii = _extract_facilities(grids)
    pii_dropped += fac_pii

    if pii_dropped > 0:
        warnings.append({"code": "PII_DROPPED", "count": pii_dropped})

    preview: dict[str, object] = {
        "parser_version": PARSER_VERSION,
        "extraction_mode": extraction_mode,
        "as_of_time": as_of_date.isoformat() if as_of_date else None,
        "cover": cover,
        "relationship_note": relationship_note,
        "people": people,
        "achievements": achievements,
        "projects": projects,
        "facilities": facilities,
        "warnings": warnings,
    }

    assert_no_pii(preview)

    stats: dict[str, object] = {
        "people": len(people),
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

    for grid in grids:
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
        for row in grid[:kv_end]:
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
                        else:
                            applicant[target] = val
                    i += 2
                else:
                    if is_pii_id_or_phone(row[i]):
                        pii_count += 1
                    i += 1

        applicant["warnings"] = applicant_warnings
        if applicant.get("name"):
            people.append(applicant)

        # 2) Parse Member table rows starting at header_row_idx + 1
        if header_row_idx is not None:
            header_row = grid[header_row_idx]
            col_map: dict[int, str] = {}
            for c_idx, cell_text in enumerate(header_row):
                lbl = normalize_label(cell_text)
                if lbl in MEMBER_HEADER_MAP:
                    col_map[c_idx] = MEMBER_HEADER_MAP[lbl]

            for row in grid[header_row_idx + 1 :]:
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
                if not raw_name or not CN_NAME_RE.match(raw_name):
                    continue
                member["warnings"] = mem_warnings
                people.append(member)

    # Deduplicate by (normalized name, normalized affiliation), keeping applicant role
    deduped: list[dict[str, object]] = []
    by_key: dict[tuple[str, str], dict[str, object]] = {}
    for person in people:
        pname = str(person.get("name") or "")
        paff = norm(str(person.get("affiliation") or ""))
        key = (norm(pname), paff)
        if key in by_key:
            existing = by_key[key]
            for field in (
                "title",
                "admin_duty",
                "tutor_role",
                "research_specialty",
                "degree",
                "affiliation",
                "email",
                "cert",
                "bio",
            ):
                if not existing.get(field) and person.get(field):
                    existing[field] = person[field]
            if person.get("project_role") == "applicant":
                existing["project_role"] = "applicant"
        else:
            by_key[key] = person
            deduped.append(person)

    return deduped, pii_count


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
    t4_text = ""
    for grid in grids:
        for row in grid:
            for cell in row:
                if any(k in cell for k in ("[学术简历]", "[前期成果]", "[承担项目]", "［学术简历］")):
                    t4_text = cell
                    break
            if t4_text:
                break
        if t4_text:
            break

    if not t4_text:
        return None, [], [], None, 0, [{"code": "SECTION_NOT_FOUND", "section": "foundation"}]

    sections: dict[str, str] = {}
    matches = list(ANCHOR_RE.finditer(t4_text))
    for idx, match in enumerate(matches):
        sec_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(t4_text)
        raw_title = match.group(2)
        body = t4_text[match.end() : sec_end].strip()
        if "学术简历" in raw_title:
            sections["resume"] = body
        elif "前期成果" in raw_title:
            sections["achievements"] = body
        elif "承担项目" == raw_title:
            sections["projects"] = body
        elif "关系" in raw_title:
            sections["relationship"] = body

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
        r"^([一-龥·]{2,10})\s*(?:[（(]第一[)）])?\s*[:：]\s*(.+)$",
        first_sentence,
    )
    if m_lead:
        first_author = normalize_person_name(m_lead.group(1))
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
        if len(parts) >= 3:
            raw_venue = parts[2]
            if raw_venue.startswith("《") and raw_venue.endswith("》"):
                venue = raw_venue[1:-1].strip()
            else:
                venue = raw_venue
        elif len(parts) == 2:
            venue = parts[1]
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
        m_author = re.match(r"^([一-龥·]{2,6})", first_sentence)
        if m_author:
            first_author = normalize_person_name(m_author.group(1))
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
                r"^(?:主持人|负责人)?[:：]?\s*([一-龥·]{2,6})\s*(?:[（(]?主持[)）]?|负责人)$",
                seg,
            )
            if m_host:
                pi_name = normalize_person_name(m_host.group(1))
                lab_role = "host"
                continue
            m_part = re.match(r"^([一-龥·]{2,6})\s*(?:参与|参加)$", seg)
            if m_part:
                pi_name = normalize_person_name(m_part.group(1))
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
            funder = seg

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
            if any(normalize_label(c) == "设备费" for c in row):
                raw_desc = row[-1] if len(row) >= 3 else joined
                scrubbed_desc, cnt = scrub_free_text(raw_desc)
                pii_total += cnt
                value_wan: str | None = None
                for c in row:
                    if re.match(r"^\d+(?:\.\d+)?$", c.strip()):
                        try:
                            dec = Decimal(c.strip())
                            if dec > 0:
                                value_wan = f"{dec:.2f}"
                        except InvalidOperation:
                            pass
                name = "人工智能计算加速设备及配套部件"
                m_name = re.search(r"所需的([^；;。，,]+)", scrubbed_desc)
                if m_name:
                    name = m_name.group(1).strip()
                usage: str | None = scrubbed_desc
                m_usage = re.search(r"用于(.+?)所需的", scrubbed_desc)
                if m_usage:
                    usage = m_usage.group(1).strip()
                category = (
                    "compute"
                    if any(k in scrubbed_desc for k in ("计算", "算力", "推理", "微调", "GPU"))
                    else "equipment"
                )
                spec = re.split(r"[；;]", scrubbed_desc)[0].strip()
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
