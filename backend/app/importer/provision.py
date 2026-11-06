"""Transactional account provisioning, asset upsert, roster generation, and RAG indexing."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import hash_password, validate_password
from app.config import Settings
from app.importer.fingerprint import (
    achievement_fp,
    facility_fp,
    person_fp,
    project_fp,
)
from app.importer.formatters import render_lab_profile_markdown
from app.importer.people import (
    assign_people_identities,
    is_valid_login_name,
    normalize_person_name,
)
from app.importer.pii import assert_no_pii
from app.models import (
    Document,
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
    User,
)
from app.rag.embedder import Embedder
from app.rag.ingest import upsert_source

_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


class ProvisionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class PersonOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    exclude: bool = False
    login_name: str | None = None
    link_user_id: uuid.UUID | None = None


class ProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of_time: date | None = None
    initial_password: str | None = None
    include_password: bool = True
    people: list[PersonOverride] = Field(default_factory=list)


def _escape_csv(value: object) -> str:
    if value is None:
        return ""
    s = str(value)
    if not s:
        return ""
    if s.startswith(_CSV_DANGEROUS_PREFIXES) or s.lstrip().startswith(_CSV_DANGEROUS_PREFIXES):
        return f"'{s}"
    return s


def render_roster_text(
    *,
    base_url: str,
    initial_password: str | None,
    include_password: bool,
    deadline_str: str,
    new_accounts: list[dict[str, object]],
    existing_accounts: list[dict[str, object]],
    oidc_mode: bool = False,
) -> str:
    if oidc_mode:
        lines = [
            "【LabAgent 账号已开通】",
            f"登录地址：{base_url}",
            "登录方式：统一身份认证 (OIDC SSO)",
            "",
        ]
    else:
        pw_line = (
            f"初始口令：{initial_password}"
            if (include_password and initial_password)
            else "初始口令：另行私发"
        )
        lines = [
            "【LabAgent 账号已开通】",
            f"登录地址：{base_url}",
            pw_line,
            f"⚠ 请在 {deadline_str} 前登录并修改口令，逾期初始口令失效，需联系管理员重置。",
            "",
        ]
    for item in new_accounts:
        name = str(item.get("name") or "")
        login_name = str(item.get("login_name") or "")
        has_email = bool(item.get("has_email"))
        suffixed = bool(item.get("suffixed"))
        affiliation = str(item.get("affiliation") or "")
        name_label = f"{name}（{affiliation}）" if (suffixed and affiliation) else name
        email_note = "（也可用本人邮箱登录）" if has_email else ""
        lines.append(f"{name_label}　登录名：{login_name}{email_note}")

    if existing_accounts:
        lines.append("")
        lines.append("以下成员已有账号，请沿用原登录方式：")
        for item in existing_accounts:
            lines.append(str(item.get("name") or ""))

    out = "\n".join(lines).strip() + "\n"
    assert_no_pii(out)
    return out


def render_roster_csv(
    *,
    initial_password: str | None,
    include_password: bool,
    deadline_str: str,
    new_accounts: list[dict[str, object]],
    existing_accounts: list[dict[str, object]],
    oidc_mode: bool = False,
) -> str:
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf)
    headers = ["姓名", "登录名", "可用邮箱登录", "单位", "账号状态", "改密截止"]
    show_pw = not oidc_mode and include_password and bool(initial_password)
    if show_pw:
        headers.append("初始口令")
    writer.writerow(headers)

    for item in new_accounts:
        row = [
            _escape_csv(item.get("name")),
            _escape_csv(item.get("login_name")),
            "是" if item.get("has_email") else "否",
            _escape_csv(item.get("affiliation") or ""),
            "新建 (OIDC SSO)" if oidc_mode else "新建",
            "" if oidc_mode else _escape_csv(deadline_str),
        ]
        if show_pw:
            row.append(_escape_csv(initial_password))
        writer.writerow(row)

    for item in existing_accounts:
        row = [
            _escape_csv(item.get("name")),
            _escape_csv(item.get("login_name") or ""),
            "是" if item.get("has_email") else "否",
            _escape_csv(item.get("affiliation") or ""),
            "已有",
            "",
        ]
        if show_pw:
            row.append("")
        writer.writerow(row)

    out = buf.getvalue()
    assert_no_pii(out)
    return out


async def provision_import_job(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    actor_id: uuid.UUID,
    body: ProvisionRequest,
    settings: Settings,
    embedder: Embedder,
) -> dict[str, object]:
    """Execute account provisioning, lab asset persistence, and RAG profile indexing."""
    job = await session.scalar(
        select(ImportJob).where(ImportJob.id == job_id).with_for_update()
    )
    if job is None:
        raise ProvisionError(404, "not_found", "导入任务不存在")
    if job.status == "provisioned":
        raise ProvisionError(409, "import_already_provisioned", "该导入任务已完成开号")
    if job.status != "parsed":
        raise ProvisionError(409, "import_not_parsed", "该导入任务当前状态不可开号")

    preview: dict[str, Any] = dict(job.preview or {})
    raw_as_of = preview.get("as_of_time")
    parsed_preview_date: date | None = None
    if isinstance(raw_as_of, str) and raw_as_of.strip():
        try:
            parsed_preview_date = date.fromisoformat(raw_as_of.strip())
        except ValueError:
            parsed_preview_date = None
    as_of_date = body.as_of_time or parsed_preview_date or job.as_of_time
    if as_of_date is None:
        raise ProvisionError(422, "as_of_time_required", "请提供本子基准时间 as_of_time")

    existing_users = list(await session.scalars(select(User)))
    existing_people = list(await session.scalars(select(LabPerson)))
    users_by_id = {u.id: u for u in existing_users}
    used_login_names = {
        u.login_name.lower() for u in existing_users if u.login_name is not None
    }

    raw_people_list = preview.get("people")
    raw_people: list[dict[str, Any]] = [
        dict(p) for p in (raw_people_list if isinstance(raw_people_list, list) else []) if isinstance(p, dict)
    ]
    if not raw_people:
        raise ProvisionError(422, "import_parse_failed", "预览中没有可开通的人员记录")

    # Recompute login_name and matches against current DB users & existing LabPerson fingerprints
    for p in raw_people:
        if p.get("tier") == "T1":
            p["cert"] = p.get("login_name")
    assign_people_identities(
        raw_people,
        existing_users=existing_users,
        existing_people=existing_people,
    )

    overrides_by_key = {ov.key: ov for ov in body.people}
    active_people: list[dict[str, Any]] = []

    for person in raw_people:
        pkey = str(person.get("key") or "")
        ov = overrides_by_key.get(pkey)
        if ov and ov.exclude:
            continue

        cur_warnings = person.get("warnings")
        warn_list = list(cur_warnings) if isinstance(cur_warnings, list) else []

        if ov and ov.link_user_id is not None:
            linked_target = users_by_id.get(ov.link_user_id)
            if linked_target is None:
                raise ProvisionError(422, "link_user_invalid", f"关联的目标用户不存在 ({pkey})")
            person["match"] = {"status": "existing", "user_id": str(linked_target.id)}
            warn_list = [
                w for w in warn_list if w not in ("LOGIN_CONFLICT", "LOGIN_NAME_REQUIRED")
            ]
            person["warnings"] = warn_list

        if ov and ov.login_name is not None:
            custom_login = ov.login_name.strip().lower()
            if not is_valid_login_name(custom_login):
                raise ProvisionError(422, "login_name_invalid", f"登录名格式不合法 ({pkey})")
            if custom_login in used_login_names:
                raise ProvisionError(409, "login_name_taken", f"登录名已被占用: {custom_login}")
            person["login_name"] = custom_login
            used_login_names.add(custom_login)
            if ov.link_user_id is None:
                cur_match = person.get("match")
                if isinstance(cur_match, dict) and cur_match.get("status") == "conflict":
                    person["match"] = {"status": "new"}
            person["fingerprint"] = person_fp(
                tier=str(person.get("tier") or "T2"),
                login_name=custom_login,
                name=normalize_person_name(str(person.get("name") or "")),
                affiliation=str(person.get("affiliation") or "").strip() or None,
            )
            warn_list = [
                w for w in warn_list if w not in ("LOGIN_CONFLICT", "LOGIN_NAME_REQUIRED")
            ]
            person["warnings"] = warn_list

        p_warnings = [str(w) for w in warn_list]
        raw_match = person.get("match")
        match_info: dict[str, Any] = raw_match if isinstance(raw_match, dict) else {}
        match_status = str(match_info.get("status") or "new")

        if match_status == "conflict" or "LOGIN_CONFLICT" in p_warnings:
            raise ProvisionError(
                422,
                "login_conflict_unresolved",
                f"人员 {pkey} 存在学工号冲突，请排除或修改登录名",
            )
        if match_status == "new":
            login_candidate = str(person.get("login_name") or "").strip().lower()
            if not is_valid_login_name(login_candidate) or "LOGIN_NAME_REQUIRED" in p_warnings:
                raise ProvisionError(
                    422,
                    "login_name_required",
                    f"人员 {pkey} 缺少有效登录名",
                )
        active_people.append(person)

    oidc_mode = bool(settings.oidc_issuer and settings.oidc_issuer.strip())
    initial_password = (
        body.initial_password.strip()
        if body.initial_password and body.initial_password.strip()
        else settings.import_default_password
    )

    if not oidc_mode:
        # Validate initial_password for every new account before touching DB
        for person in active_people:
            raw_match = person.get("match")
            match_info = raw_match if isinstance(raw_match, dict) else {}
            if str(match_info.get("status") or "new") == "new":
                p_email = str(person.get("email") or "").strip().lower() or None
                try:
                    validate_password(initial_password, p_email)
                except ValueError as exc:
                    raise ProvisionError(422, "password_policy_violation", str(exc)) from exc

    now_utc = datetime.now(UTC)
    deadline_utc = now_utc + timedelta(days=max(1, settings.initial_password_ttl_days))
    try:
        tz = ZoneInfo(settings.display_tz)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    deadline_display = deadline_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")

    # Pre-load existing fingerprints to implement ON CONFLICT DO NOTHING cleanly
    existing_person_fps = set(await session.scalars(select(LabPerson.entity_fingerprint)))
    existing_person_user_ids = {
        uid for uid in await session.scalars(select(LabPerson.user_id)) if uid is not None
    }
    existing_ach_fps = set(await session.scalars(select(LabAchievement.entity_fingerprint)))
    existing_proj_fps = set(await session.scalars(select(LabProject.entity_fingerprint)))
    existing_fac_fps = set(await session.scalars(select(LabFacility.entity_fingerprint)))

    users_created = 0
    users_linked = 0
    people_inserted = 0
    people_skipped = 0
    new_roster_items: list[dict[str, object]] = []
    existing_roster_items: list[dict[str, object]] = []

    pw_hash = (
        hash_password(f"OIDC-Only-{uuid.uuid4().hex}#99")
        if oidc_mode
        else hash_password(initial_password)
    )

    try:
        for person in active_people:
            pname = normalize_person_name(str(person.get("name") or ""))
            p_email = str(person.get("email") or "").strip().lower() or None
            p_login = str(person.get("login_name") or "").strip().lower() or None
            p_tier = str(person.get("tier") or "T2")
            p_aff = str(person.get("affiliation") or "").strip() or None
            raw_w = person.get("warnings")
            p_warnings = [str(w) for w in (raw_w if isinstance(raw_w, list) else [])]

            raw_match = person.get("match")
            match_info = raw_match if isinstance(raw_match, dict) else {}
            match_status = str(match_info.get("status") or "new")

            user_id: uuid.UUID
            if match_status == "existing" and match_info.get("user_id"):
                target_uid = uuid.UUID(str(match_info["user_id"]))
                existing_user = users_by_id.get(target_uid)
                if existing_user is None:
                    raise ProvisionError(422, "link_user_invalid", "关联用户不存在")
                # Never downgrade role or overwrite password of existing user!
                user_id = existing_user.id
                users_linked += 1
                existing_roster_items.append(
                    {
                        "name": pname,
                        "login_name": existing_user.login_name or existing_user.email or "",
                        "has_email": existing_user.email is not None,
                        "affiliation": p_aff,
                    }
                )
            else:
                # Hardcode role="member" — never auto-assign admin!
                new_user = User(
                    email=p_email,
                    login_name=p_login,
                    display_name=pname,
                    password_hash=pw_hash,
                    role="member",
                    is_active=True,
                    must_change_password_by=None if oidc_mode else deadline_utc,
                )
                session.add(new_user)
                await session.flush()
                user_id = new_user.id
                users_created += 1
                new_roster_items.append(
                    {
                        "name": pname,
                        "login_name": p_login,
                        "has_email": p_email is not None,
                        "suffixed": "LOGIN_NAME_SUFFIXED" in p_warnings,
                        "affiliation": p_aff,
                    }
                )

            fp_val = str(person.get("fingerprint") or "") or person_fp(
                tier=p_tier,
                login_name=p_login,
                name=pname,
                affiliation=p_aff,
            )
            if fp_val in existing_person_fps or user_id in existing_person_user_ids:
                people_skipped += 1
            else:
                existing_person_fps.add(fp_val)
                existing_person_user_ids.add(user_id)
                proj_role = (
                    "applicant" if person.get("project_role") == "applicant" else "member"
                )
                session.add(
                    LabPerson(
                        user_id=user_id,
                        name=pname,
                        email=p_email,
                        title=str(person.get("title") or "").strip() or None,
                        admin_duty=str(person.get("admin_duty") or "").strip() or None,
                        tutor_role=str(person.get("tutor_role") or "").strip() or None,
                        research_specialty=(
                            str(person.get("research_specialty") or "").strip() or None
                        ),
                        degree=str(person.get("degree") or "").strip() or None,
                        affiliation=p_aff,
                        project_role=proj_role,
                        bio=str(person.get("bio") or "").strip() or None,
                        entity_fingerprint=fp_val,
                        as_of_time=as_of_date,
                        source_import_id=job.id,
                    )
                )
                people_inserted += 1

        # Achievements
        ach_inserted = 0
        ach_skipped = 0
        raw_ach_list = preview.get("achievements")
        raw_achievements: list[dict[str, Any]] = [
            dict(a)
            for a in (raw_ach_list if isinstance(raw_ach_list, list) else [])
            if isinstance(a, dict)
        ]
        for ach in raw_achievements:
            kind = str(ach.get("kind") or "other")
            title = str(ach.get("title") or "").strip()
            raw_text = str(ach.get("raw_text") or title).strip()
            year_val = int(ach["year"]) if isinstance(ach.get("year"), int) else None
            pq = str(ach.get("parse_quality") or "structured")
            fp_val = str(ach.get("fingerprint") or "") or achievement_fp(
                kind=kind,
                title=title,
                year=year_val,
                raw_text=raw_text,
                parse_quality=pq,
            )
            if fp_val in existing_ach_fps:
                ach_skipped += 1
                continue
            existing_ach_fps.add(fp_val)
            session.add(
                LabAchievement(
                    kind=kind,
                    title=title,
                    first_author=str(ach.get("first_author") or "").strip() or None,
                    authors_text=str(ach.get("authors_text") or "").strip() or None,
                    venue=str(ach.get("venue") or "").strip() or None,
                    year=year_val,
                    support_note=str(ach.get("support_note") or "").strip() or None,
                    raw_text=raw_text,
                    parse_quality=pq,
                    entity_fingerprint=fp_val,
                    as_of_time=as_of_date,
                    source_import_id=job.id,
                )
            )
            ach_inserted += 1

        # Projects
        proj_inserted = 0
        proj_skipped = 0
        raw_proj_list = preview.get("projects")
        raw_projects: list[dict[str, Any]] = [
            dict(j)
            for j in (raw_proj_list if isinstance(raw_proj_list, list) else [])
            if isinstance(j, dict)
        ]
        for proj in raw_projects:
            title = str(proj.get("title") or "").strip()
            funder = str(proj.get("funder") or "").strip() or None
            grant_no = str(proj.get("grant_no") or "").strip() or None
            pi_name = str(proj.get("pi_name") or "").strip() or None
            lab_role = str(proj.get("lab_role") or "host")
            if lab_role not in ("host", "participant", "unknown"):
                lab_role = "host"
            start_d = _parse_iso_date(proj.get("start_date"))
            end_d = _parse_iso_date(proj.get("end_date"))
            amt_wan = _parse_decimal(proj.get("amount_wan"))
            status = str(proj.get("status") or "unknown")
            status_source = str(proj.get("status_source") or "explicit")
            if status_source == "inferred" and end_d is not None:
                status = "completed" if end_d < as_of_date else "ongoing"
            raw_text = str(proj.get("raw_text") or title).strip()
            fp_val = str(proj.get("fingerprint") or "") or project_fp(
                grant_no=grant_no,
                title=title,
                funder=funder,
            )
            if fp_val in existing_proj_fps:
                proj_skipped += 1
                continue
            existing_proj_fps.add(fp_val)
            session.add(
                LabProject(
                    title=title,
                    funder=funder,
                    grant_no=grant_no,
                    pi_name=pi_name,
                    lab_role=lab_role,
                    start_date=start_d,
                    end_date=end_d,
                    amount_wan=amt_wan,
                    status=status,
                    status_source=status_source,
                    relationship_note=(
                        str(proj.get("relationship_note") or "").strip() or None
                    ),
                    raw_text=raw_text,
                    entity_fingerprint=fp_val,
                    as_of_time=as_of_date,
                    source_import_id=job.id,
                )
            )
            proj_inserted += 1

        # Facilities
        fac_inserted = 0
        fac_skipped = 0
        raw_fac_list = preview.get("facilities")
        raw_facilities: list[dict[str, Any]] = [
            dict(f)
            for f in (raw_fac_list if isinstance(raw_fac_list, list) else [])
            if isinstance(f, dict)
        ]
        for fac in raw_facilities:
            category = str(fac.get("category") or "equipment")
            name = str(fac.get("name") or "").strip()
            spec = str(fac.get("spec") or "").strip() or None
            val_wan = _parse_decimal(fac.get("value_wan"))
            usage = str(fac.get("usage_scenario") or "").strip() or None
            raw_text = str(fac.get("raw_text") or name).strip()
            fp_val = str(fac.get("fingerprint") or "") or facility_fp(
                category=category,
                name=name,
                spec=spec,
            )
            if fp_val in existing_fac_fps:
                fac_skipped += 1
                continue
            existing_fac_fps.add(fp_val)
            session.add(
                LabFacility(
                    category=category,
                    name=name,
                    spec=spec,
                    value_wan=val_wan,
                    usage_scenario=usage,
                    raw_text=raw_text,
                    entity_fingerprint=fp_val,
                    as_of_time=as_of_date,
                    source_import_id=job.id,
                )
            )
            fac_inserted += 1

        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ProvisionError(
            409,
            "login_name_taken",
            "开号过程中登录名或邮箱发生并发冲突，请刷新后重试",
        ) from exc

    # Render canonical Markdown from all lab_* rows and index into RAG
    all_people = list(
        await session.scalars(
            select(LabPerson).order_by(
                LabPerson.project_role.asc(), LabPerson.created_at.asc()
            )
        )
    )
    all_projects = list(
        await session.scalars(select(LabProject).order_by(LabProject.created_at.asc()))
    )
    all_achievements = list(
        await session.scalars(
            select(LabAchievement).order_by(LabAchievement.created_at.asc())
        )
    )
    all_facilities = list(
        await session.scalars(select(LabFacility).order_by(LabFacility.created_at.asc()))
    )

    raw_cover = preview.get("cover")
    cover_dict: dict[str, Any] = raw_cover if isinstance(raw_cover, dict) else {}
    project_title = str(cover_dict.get("project_title") or "") or job.original_filename
    rel_note = str(preview.get("relationship_note") or "").strip() or None

    profile_md = render_lab_profile_markdown(
        project_title=project_title,
        as_of_time=as_of_date,
        people=all_people,
        projects=all_projects,
        achievements=all_achievements,
        facilities=all_facilities,
        relationship_note=rel_note,
    )
    doc_key = f"lab-profile-{job.id}"
    await upsert_source(
        session,
        embedder=embedder,
        owner_id=actor_id,
        source=f"import-{job.id.hex[:24]}",
        records=[
            (
                {
                    "key": doc_key,
                    "title": f"课题组资料库 · {project_title}",
                    "collection": "project",
                    "visibility": "lab",
                    "source_format": "markdown",
                },
                profile_md,
            )
        ],
    )
    rag_doc = await session.scalar(select(Document).where(Document.doc_key == doc_key))

    stats_payload: dict[str, object] = {
        "users_created": users_created,
        "users_linked": users_linked,
        "people_inserted": people_inserted,
        "people_skipped": people_skipped,
        "achievements_inserted": ach_inserted,
        "achievements_skipped": ach_skipped,
        "projects_inserted": proj_inserted,
        "projects_skipped": proj_skipped,
        "facilities_inserted": fac_inserted,
        "facilities_skipped": fac_skipped,
        "rag_doc_key": doc_key,
    }

    job.as_of_time = as_of_date
    job.status = "provisioned"
    job.provisioned_at = now_utc
    job.provisioned_by = actor_id
    job.rag_document_id = rag_doc.id if rag_doc is not None else None
    job.stats = stats_payload

    await session.flush()

    roster_text = render_roster_text(
        base_url=settings.public_base_url,
        initial_password=None if oidc_mode else initial_password,
        include_password=body.include_password,
        deadline_str=deadline_display,
        new_accounts=new_roster_items,
        existing_accounts=existing_roster_items,
        oidc_mode=oidc_mode,
    )
    roster_csv = render_roster_csv(
        initial_password=None if oidc_mode else initial_password,
        include_password=body.include_password,
        deadline_str=deadline_display,
        new_accounts=new_roster_items,
        existing_accounts=existing_roster_items,
        oidc_mode=oidc_mode,
    )

    return {
        "job_id": str(job.id),
        "status": "provisioned",
        "as_of_time": as_of_date.isoformat(),
        "deadline": None if oidc_mode else deadline_utc.isoformat(),
        "initial_password": (
            None if (oidc_mode or not body.include_password) else initial_password
        ),
        "rag_document_id": str(rag_doc.id) if rag_doc is not None else None,
        "rag_doc_key": doc_key,
        "stats": stats_payload,
        "roster_text": roster_text,
        "roster_csv": roster_csv,
    }


def _parse_iso_date(val: object) -> date | None:
    if val is None:
        return None
    if isinstance(val, date):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _parse_decimal(val: object) -> Decimal | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None
