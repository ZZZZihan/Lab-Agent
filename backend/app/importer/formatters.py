"""Standard grant proposal formatters for lab assets and RAG Markdown profile rendering."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.importer.pii import assert_no_pii

ACHIEVEMENT_KIND_LABELS: dict[str, str] = {
    "report": "研究报告/资政报告",
    "exhibition": "成果展",
    "paper": "论文",
    "patent": "发明专利",
    "software": "软件著作权",
    "award": "科研奖励",
    "book": "学术专著",
    "other": "其他成果",
}

PROJECT_STATUS_LABELS: dict[str, str] = {
    "ongoing": "在研",
    "completed": "已结题",
    "unknown": "状态未注明",
}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def format_person_application(person: Any, *, include_bio: bool = False) -> str:
    """Format a lab person into standard grant proposal 1-line summary (or with bio)."""
    name = str(_get(person, "name") or "").strip()
    degree = str(_get(person, "degree") or "").strip()
    affiliation = str(_get(person, "affiliation") or "").strip()
    title = str(_get(person, "title") or "").strip()
    admin_duty = str(_get(person, "admin_duty") or "").strip()
    tutor_role = str(_get(person, "tutor_role") or "").strip()
    specialty = str(_get(person, "research_specialty") or "").strip()
    bio = str(_get(person, "bio") or "").strip()

    parts: list[str] = [name]
    if degree:
        parts.append(degree)
    role_bits: list[str] = []
    if affiliation:
        role_bits.append(affiliation)
    if title and title != "无":
        role_bits.append(title)
    if admin_duty and admin_duty != "无":
        role_bits.append(admin_duty)
    if tutor_role and tutor_role != "无":
        role_bits.append(tutor_role)
    if role_bits:
        parts.append("、".join(role_bits))
    elif title == "无":
        parts.append("研究生")
    if specialty:
        parts.append(f"研究专长：{specialty}")
    line = "，".join(parts) + "。"
    if include_bio and bio:
        return f"{line.rstrip('。')}；{bio}"
    return line


def format_project_application(project: Any, *, include_relationship: bool = False) -> str:
    """Format a lab project into standard grant proposal citation text."""
    title = str(_get(project, "title") or "").strip()
    grant_no = str(_get(project, "grant_no") or "").strip()
    pi_name = str(_get(project, "pi_name") or "").strip()
    lab_role = str(_get(project, "lab_role") or "unknown").strip()
    funder = str(_get(project, "funder") or "").strip()
    status = str(_get(project, "status") or "unknown").strip()
    start_val = _get(project, "start_date")
    relationship_note = str(_get(project, "relationship_note") or "").strip()

    wrapped_title = title if (title.startswith("《") and title.endswith("》")) else f"《{title}》"
    if grant_no:
        wrapped_title = f"{wrapped_title}（{grant_no}）"

    segments: list[str] = []
    if pi_name:
        if lab_role == "participant":
            role_label = pi_name if pi_name.endswith("参与") else f"{pi_name}参与"
        elif lab_role == "host":
            role_label = pi_name if pi_name.endswith("主持") else f"{pi_name}主持"
        else:
            role_label = pi_name
        segments.append(f"{role_label}：{wrapped_title}")
    else:
        segments.append(wrapped_title)

    if funder:
        segments.append(funder)

    start_str = _format_ym(start_val)
    if start_str:
        segments.append(f"{start_str}立项")

    status_cn = PROJECT_STATUS_LABELS.get(status, "状态未注明")
    segments.append(status_cn)

    line = "，".join(segments) + "。"
    if include_relationship and relationship_note:
        line = f"{line}与本课题关系：{relationship_note}"
    return line


def format_achievement_application(
    achievement: Any,
    *,
    include_support: bool = True,
) -> str:
    """Format a lab achievement into standard grant proposal citation text."""
    first_author = str(_get(achievement, "first_author") or "").strip()
    title = str(_get(achievement, "title") or "").strip()
    kind = str(_get(achievement, "kind") or "other").strip()
    venue = str(_get(achievement, "venue") or "").strip()
    year = _get(achievement, "year")
    support_note = str(_get(achievement, "support_note") or "").strip()

    if "《" in title and "》" in title:
        title_part = title
    else:
        title_part = f"《{title}》"

    head = f"{first_author}（第一）：{title_part}" if first_author else title_part
    segments: list[str] = [head]
    kind_cn = ACHIEVEMENT_KIND_LABELS.get(kind, "其他成果")
    segments.append(kind_cn)
    if venue:
        segments.append(venue)
    if year:
        segments.append(f"{year}年")

    line = "，".join(segments) + "。"
    if include_support and support_note:
        if not support_note.endswith("。"):
            support_note = support_note + "。"
        line = f"{line}{support_note}"
    return line


def format_facility_application(facility: Any) -> str:
    """Format a lab facility into standard grant proposal condition text."""
    name = str(_get(facility, "name") or "").strip()
    spec = str(_get(facility, "spec") or "").strip()
    value_wan = _get(facility, "value_wan")
    usage = str(_get(facility, "usage_scenario") or "").strip()
    raw_text = str(_get(facility, "raw_text") or "").strip()

    parts: list[str] = []
    if spec and spec != name:
        parts.append(f"{name}（{spec}）")
    else:
        parts.append(name)
    if value_wan is not None and str(value_wan).strip():
        parts.append(f"经费/原值 {value_wan} 万元")
    if usage and usage != spec:
        parts.append(f"用于{usage}")
    if raw_text and ("；" in raw_text or ";" in raw_text):
        sep = "；" if "；" in raw_text else ";"
        extra = raw_text.split(sep, 1)[1].strip().rstrip("。")
        if extra and extra not in "，".join(parts):
            parts.append(extra)
    return "，".join(parts) + "。"


def render_lab_profile_markdown(
    *,
    project_title: str | None,
    as_of_time: date | str | None,
    people: list[Any],
    projects: list[Any],
    achievements: list[Any],
    facilities: list[Any],
    relationship_note: str | None = None,
) -> str:
    """Render canonical Markdown document for RAG indexing (`lab-profile-{job_id}`)."""
    title_str = project_title or "课题组资料库档案"
    as_of_str = (
        as_of_time.isoformat()
        if isinstance(as_of_time, date)
        else (str(as_of_time) if as_of_time else "未知")
    )

    ongoing_projects = [p for p in projects if str(_get(p, "status")) == "ongoing"]
    completed_projects = [p for p in projects if str(_get(p, "status")) == "completed"]

    rel_text = relationship_note
    if not rel_text:
        for proj in projects:
            rn = str(_get(proj, "relationship_note") or "").strip()
            if rn:
                rel_text = rn
                break

    lines: list[str] = [
        f"# 课题组资料库：{title_str}",
        "",
        f"基准时间：{as_of_str}。本档案包含课题组团队成员（{len(people)}人）、"
        f"承担项目（{len(projects)}项，其中在研{len(ongoing_projects)}项、已结题{len(completed_projects)}项）、"
        f"前期成果（{len(achievements)}项）及设备与支撑条件（{len(facilities)}项）。",
        "",
        "## 课题组概况与项目清单",
    ]
    if ongoing_projects:
        lines.append(
            f"课题组概况与项目清单之在研项目清单（共{len(ongoing_projects)}项）："
            + "；".join(format_project_application(p).rstrip("。") for p in ongoing_projects)
            + "。"
        )
    if completed_projects:
        lines.append(
            f"课题组概况与项目清单之已结题项目清单（共{len(completed_projects)}项）："
            + "；".join(format_project_application(p).rstrip("。") for p in completed_projects)
            + "。"
        )
    if rel_text:
        lines.append(
            f"课题组概况与项目清单之本课题与已承担项目的关系：{rel_text.rstrip('。')}。"
        )
    lines.append("")

    if projects:
        lines.append("## 课题组概况与项目清单（承担全部项目汇总）")
        brief_items: list[str] = []
        for p in projects:
            pt = str(_get(p, "title") or "").strip()
            gno = str(_get(p, "grant_no") or "").strip()
            pi = str(_get(p, "pi_name") or "").strip()
            role_lbl = "参与" if str(_get(p, "lab_role")) == "participant" else "主持"
            st_lbl = PROJECT_STATUS_LABELS.get(str(_get(p, "status") or "unknown"), "状态未注明")
            brief_items.append(f"{pi}{role_lbl}《{pt}》（{gno}，{st_lbl}）")
        lines.append(
            f"课题组概况与项目清单之课题组承担全部项目汇总（共{len(projects)}项）："
            + "；".join(brief_items)
            + "。"
        )
        lines.append("")

    if people:
        lines.append("## 团队成员")
        for person in people:
            pname = str(_get(person, "name") or "")
            prole = (
                "课题负责人/申请人"
                if _get(person, "project_role") == "applicant"
                else "项目组成员"
            )
            base_line = format_person_application(person).rstrip("。")
            bio = str(_get(person, "bio") or "").strip()
            if bio:
                bio_clean = bio.replace("。", "；").rstrip("；")
                lines.append(
                    f"团队成员 {pname}（{prole}）：{base_line}；个人申报书简介与学术简历：{bio_clean}。"
                )
            else:
                lines.append(f"团队成员 {pname}（{prole}）：{base_line}。")
        lines.append("")

    if projects:
        lines.append("## 承担项目")
        for idx, proj in enumerate(projects):
            gno = str(_get(proj, "grant_no") or "").strip()
            amt = _get(proj, "amount_wan")
            p_line = format_project_application(proj, include_relationship=False).rstrip("。")
            if amt is not None and str(amt).strip():
                p_line += f"，经费{amt}万元"
            if idx == 0 and rel_text:
                p_line += f"；与本课题关系：{rel_text.rstrip('。')}"
            prefix = f"承担项目 {gno}：" if gno else "承担项目："
            lines.append(f"{prefix}{p_line}。")
        lines.append("")

    if achievements:
        lines.append("## 前期成果")
        papers = [a for a in achievements if str(_get(a, "kind")) == "paper"]
        for ach in achievements:
            akind = ACHIEVEMENT_KIND_LABELS.get(
                str(_get(ach, "kind") or "other"), "其他成果"
            )
            a_base = format_achievement_application(ach, include_support=False).rstrip("。")
            sup = str(_get(ach, "support_note") or "").strip().rstrip("。")
            if sup:
                lines.append(f"前期成果（{akind}）：{a_base}；{sup}。")
            else:
                lines.append(f"前期成果（{akind}）：{a_base}。")
        if len(papers) > 1:
            paper_items = [
                format_achievement_application(p, include_support=False).rstrip("。")
                for p in papers
            ]
            lines.append(
                f"前期成果之发表学术论文成果汇总（共{len(papers)}篇）："
                + "；".join(paper_items)
                + "。"
            )
        lines.append("")

    if facilities:
        lines.append("## 设备与支撑条件")
        for fac in facilities:
            lines.append(f"设备与支撑条件（实验室算力设备）：{format_facility_application(fac)}")
        lines.append("")

    md = "\n".join(lines).strip() + "\n"
    assert_no_pii(md)
    return md


def _format_ym(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, date):
        if val.month == 1 and val.day == 1:
            return f"{val.year}年"
        return f"{val.year}年{val.month}月"
    s = str(val).strip()
    if len(s) >= 7 and s[4] == "-":
        y = s[:4]
        m = int(s[5:7])
        return f"{y}年{m}月"
    return s
