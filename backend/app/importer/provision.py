"""Transactional account provisioning, asset upsert, roster generation, and RAG indexing."""

from __future__ import annotations

import base64
import csv
import io
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.auth import hash_password, validate_password
from app.config import Settings
from app.importer.fingerprint import (
    achievement_fp,
    facility_fp,
    norm,
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
from app.importer.signature import persist_import_signatures
from app.models import (
    Chunk,
    Document,
    DocumentACL,
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
    skip_signature_ids: list[str] = Field(default_factory=list)
    exclude_keys: list[str] = Field(default_factory=list)


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
    password_notice: str | None = None,
) -> str:
    if oidc_mode:
        lines = [
            "【LabAgent 账号已开通】",
            f"登录地址：{base_url}",
            "登录方式：统一身份认证 (OIDC SSO)",
            "",
        ]
    else:
        if password_notice is not None:
            pw_line = f"初始口令：{password_notice}"
        elif include_password and initial_password:
            pw_line = f"初始口令：{initial_password}"
        else:
            pw_line = "初始口令：另行私发"
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
    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
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
    as_of_date = body.as_of_time or job.as_of_time or parsed_preview_date
    if as_of_date is None:
        raise ProvisionError(422, "as_of_time_required", "请提供本子基准时间 as_of_time")

    existing_users = list(await session.scalars(select(User)))
    existing_people = list(
        await session.scalars(select(LabPerson).with_for_update())
    )
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
    exclude_key_set = {str(k).strip() for k in body.exclude_keys if str(k).strip()}
    active_people: list[dict[str, Any]] = []

    for person in raw_people:
        pkey = str(person.get("key") or person.get("temp_id") or "")
        ov = overrides_by_key.get(pkey)
        if (
            (ov and ov.exclude)
            or pkey in exclude_key_set
            or person.get("exclude") is True
            or person.get("excluded") is True
            or person.get("include") is False
            or person.get("imported") is False
        ):
            person["exclude"] = True
            continue
        person["exclude"] = False

        cur_warnings = person.get("warnings")
        warn_list = list(cur_warnings) if isinstance(cur_warnings, list) else []

        if ov and ov.link_user_id is not None:
            linked_target = users_by_id.get(ov.link_user_id)
            if linked_target is None:
                raise ProvisionError(422, "link_user_invalid", f"关联的目标用户不存在 ({pkey})")
            person["match"] = {"status": "existing", "user_id": str(linked_target.id)}
            if linked_target.login_name:
                person["login_name"] = linked_target.login_name
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
    existing_people_by_fp: dict[str, LabPerson] = {
        lp.entity_fingerprint: lp for lp in existing_people if lp.entity_fingerprint
    }
    existing_people_by_uid: dict[uuid.UUID, LabPerson] = {
        lp.user_id: lp for lp in existing_people if lp.user_id is not None
    }
    existing_people_by_name_aff: dict[tuple[str, str], list[LabPerson]] = {}
    for lp in existing_people:
        na_key = (normalize_person_name(lp.name), norm(lp.affiliation or ""))
        existing_people_by_name_aff.setdefault(na_key, []).append(lp)

    existing_person_fps = set(existing_people_by_fp.keys())
    existing_person_user_ids = set(existing_people_by_uid.keys())
    existing_ach_fps = set(await session.scalars(select(LabAchievement.entity_fingerprint)))
    existing_proj_fps = set(await session.scalars(select(LabProject.entity_fingerprint)))
    existing_fac_fps = set(await session.scalars(select(LabFacility.entity_fingerprint)))

    users_created = 0
    users_linked = 0
    people_inserted = 0
    people_skipped = 0
    new_roster_items: list[dict[str, object]] = []
    existing_roster_items: list[dict[str, object]] = []
    inserted_people: list[LabPerson] = []
    linked_existing_people: list[LabPerson] = []
    imported_people_by_key: dict[str, LabPerson] = {}
    ambiguous_person_keys: set[str] = set()
    inserted_achievements: list[LabAchievement] = []
    inserted_projects: list[LabProject] = []
    inserted_facilities: list[LabFacility] = []

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
                        "suffixed": False,
                        "affiliation": p_aff,
                        "account_status": "existing",
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
                        "account_status": "new",
                    }
                )

            pkey = str(person.get("temp_id") or person.get("key") or "")
            fp_val = str(person.get("fingerprint") or "") or person_fp(
                tier=p_tier,
                login_name=p_login,
                name=pname,
                affiliation=p_aff,
                email=p_email,
            )
            na_candidates = existing_people_by_name_aff.get(
                (pname, norm(p_aff or "")), []
            )
            if (
                len(na_candidates) >= 2
                and p_tier == "T2"
                and not p_email
                and fp_val not in existing_people_by_fp
            ):
                ambiguous_person_keys.add(pkey)

            if fp_val in existing_person_fps or user_id in existing_person_user_ids:
                people_skipped += 1
                matched_existing_lp = existing_people_by_fp.get(
                    fp_val
                ) or existing_people_by_uid.get(user_id)
                if matched_existing_lp is not None:
                    if (
                        matched_existing_lp.user_id is None
                        and user_id is not None
                        and user_id not in existing_person_user_ids
                    ):
                        matched_existing_lp.user_id = user_id
                        existing_person_user_ids.add(user_id)
                        existing_people_by_uid[user_id] = matched_existing_lp
                        linked_existing_people.append(matched_existing_lp)
                    if pkey:
                        imported_people_by_key[pkey] = matched_existing_lp
            else:
                existing_person_fps.add(fp_val)
                existing_person_user_ids.add(user_id)
                proj_role = (
                    "applicant" if person.get("project_role") == "applicant" else "member"
                )
                new_person = LabPerson(
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
                session.add(new_person)
                inserted_people.append(new_person)
                existing_people_by_fp[fp_val] = new_person
                existing_people_by_uid[user_id] = new_person
                if pkey:
                    imported_people_by_key[pkey] = new_person
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
            new_ach = LabAchievement(
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
            session.add(new_ach)
            inserted_achievements.append(new_ach)
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
            new_proj = LabProject(
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
            session.add(new_proj)
            inserted_projects.append(new_proj)
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
            new_fac = LabFacility(
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
            session.add(new_fac)
            inserted_facilities.append(new_fac)
            fac_inserted += 1

        await session.flush()

        from app.importer.provenance import (
            _find_tail_writer_job_id,
            record_entity_import_provenance,
        )

        for p_ent in [*inserted_people, *linked_existing_people]:
            await record_entity_import_provenance(
                session,
                entity_type="person",
                entity=p_ent,
                job_id=job.id,
                as_of_time=as_of_date,
                recorded_by=actor_id,
            )
        for a_ent in inserted_achievements:
            await record_entity_import_provenance(
                session,
                entity_type="achievement",
                entity=a_ent,
                job_id=job.id,
                as_of_time=as_of_date,
                recorded_by=actor_id,
            )
        for j_ent in inserted_projects:
            await record_entity_import_provenance(
                session,
                entity_type="project",
                entity=j_ent,
                job_id=job.id,
                as_of_time=as_of_date,
                recorded_by=actor_id,
            )
        for f_ent in inserted_facilities:
            await record_entity_import_provenance(
                session,
                entity_type="facility",
                entity=f_ent,
                job_id=job.id,
                as_of_time=as_of_date,
                recorded_by=actor_id,
            )

        inserted_people_ids = {p_ent.id for p_ent in inserted_people}
        existing_target_ids = {
            p_ent.id
            for p_ent in imported_people_by_key.values()
            if p_ent.id not in inserted_people_ids
        }
        existing_target_people: list[LabPerson] = []
        pre_sig_snapshots: dict[uuid.UUID, dict[str, str | None]] = {}
        if existing_target_ids:
            existing_target_people = list(
                await session.scalars(
                    select(LabPerson)
                    .options(undefer(LabPerson.signature_data))
                    .where(LabPerson.id.in_(existing_target_ids))
                    .with_for_update()
                )
            )
            for p_existing in existing_target_people:
                await session.refresh(
                    p_existing,
                    attribute_names=[
                        "signature_data",
                        "signature_mime",
                        "signature_sha256",
                        "signature_updated_at",
                        "signature_source",
                    ],
                )
                pre_sig_snapshots[p_existing.id] = {
                    "signature_data_b64": (
                        base64.b64encode(p_existing.signature_data).decode("ascii")
                        if p_existing.signature_data is not None
                        else None
                    ),
                    "signature_mime": p_existing.signature_mime,
                    "signature_sha256": p_existing.signature_sha256,
                    "signature_updated_at": (
                        p_existing.signature_updated_at.isoformat()
                        if p_existing.signature_updated_at is not None
                        else None
                    ),
                    "signature_source": p_existing.signature_source,
                }

        existing_stats_blobs = (
            (job.stats or {}).get("_signature_blobs")
            if isinstance(job.stats, dict)
            else None
        )
        raw_preview_blobs = preview.get("_signature_blobs")
        effective_sig_blobs: dict[str, object] = {}
        if isinstance(raw_preview_blobs, dict) and raw_preview_blobs:
            effective_sig_blobs = dict(raw_preview_blobs)
        elif isinstance(existing_stats_blobs, dict) and existing_stats_blobs:
            effective_sig_blobs = dict(existing_stats_blobs)
        sig_preview = (
            {**preview, "_signature_blobs": effective_sig_blobs}
            if effective_sig_blobs
            and not (isinstance(raw_preview_blobs, dict) and raw_preview_blobs)
            else preview
        )
        sig_result, sig_extra_warnings = persist_import_signatures(
            preview=sig_preview,
            imported_people_by_key=imported_people_by_key,
            skip_signature_ids=body.skip_signature_ids,
            ambiguous_keys=ambiguous_person_keys,
        )
        changed_sig_snapshots: dict[str, dict[str, str | None]] = {}
        other_merged_jobs = list(
            await session.scalars(
                select(ImportJob)
                .where(
                    ImportJob.id != job.id,
                    ImportJob.status.in_(("merged", "provisioned")),
                )
                .order_by(ImportJob.created_at.asc(), ImportJob.id.asc())
                .with_for_update()
            )
        )
        for p_existing in existing_target_people:
            pid_str = str(p_existing.id)
            snap = pre_sig_snapshots.get(p_existing.id)
            sig_bound_by_this_job = bool(
                getattr(p_existing, "_sig_bound_by_import", False)
            ) or (
                snap is not None
                and (
                    p_existing.signature_sha256 != snap["signature_sha256"]
                    or p_existing.signature_source != snap["signature_source"]
                )
            )
            if sig_bound_by_this_job and snap is not None:
                tail_id = (
                    _find_tail_writer_job_id(
                        other_merged_jobs, pid_str, snap["signature_sha256"]
                    )
                    if snap["signature_source"] == "docx_import"
                    else None
                )
                changed_sig_snapshots[pid_str] = {
                    **snap,
                    "prev_writer_job_id": tail_id,
                    "applied_sha256": p_existing.signature_sha256,
                }
        for p_new in inserted_people:
            if p_new.signature_sha256 is not None:
                changed_sig_snapshots[str(p_new.id)] = {
                    "signature_data_b64": None,
                    "signature_mime": None,
                    "signature_sha256": None,
                    "signature_updated_at": None,
                    "signature_source": None,
                    "prev_writer_job_id": None,
                    "applied_sha256": p_new.signature_sha256,
                }
        cleaned_preview = {k: v for k, v in preview.items() if k != "_signature_blobs"}
        if sig_extra_warnings:
            cur_prev_warnings = list(cleaned_preview.get("warnings") or [])
            cur_prev_warnings.extend(sig_extra_warnings)
            cleaned_preview["warnings"] = cur_prev_warnings

        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise ProvisionError(
            409,
            "login_name_taken",
            "开号过程中登录名或邮箱发生并发冲突，请刷新后重试",
        ) from exc

    roster_rows: list[dict[str, object]] = [
        *new_roster_items,
        *existing_roster_items,
    ]
    cleaned_preview["as_of_time"] = as_of_date.isoformat()
    cleaned_preview["people"] = raw_people
    cleaned_preview["provisioned_roster_rows"] = roster_rows
    assert_no_pii(cleaned_preview)
    job.preview = cleaned_preview

    job.as_of_time = as_of_date
    job.status = "provisioned"
    job.provisioned_at = now_utc
    job.provisioned_by = actor_id

    doc_key, rag_doc = await sync_lab_profile_rag(
        session,
        embedder=embedder,
        actor_id=actor_id,
        job=job,
    )

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
        "roster_rows": roster_rows,
        "signature_result": sig_result,
    }
    public_stats: dict[str, object] = dict(stats_payload)
    if changed_sig_snapshots:
        stats_payload["_pre_merge_signatures"] = changed_sig_snapshots
    if effective_sig_blobs:
        stats_payload["_signature_blobs"] = effective_sig_blobs

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
        "stats": public_stats,
        "signature_result": sig_result,
        "warnings": sig_extra_warnings,
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


async def sync_lab_profile_rag(
    session: AsyncSession,
    *,
    embedder: Embedder,
    actor_id: uuid.UUID,
    job: ImportJob | None = None,
) -> tuple[str, Document | None]:
    """Render canonical Markdown from all `lab_*` rows and upsert into RAG index."""
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
        await session.scalars(
            select(LabFacility).order_by(LabFacility.created_at.asc())
        )
    )

    target_job = job
    if target_job is None:
        target_job = await session.scalar(
            select(ImportJob)
            .where(ImportJob.status.in_(("provisioned", "merged")))
            .order_by(ImportJob.provisioned_at.desc(), ImportJob.created_at.desc())
            .limit(1)
        )

    as_of_dates = [
        d
        for d in (
            [p.as_of_time for p in all_people]
            + [j.as_of_time for j in all_projects]
            + [a.as_of_time for a in all_achievements]
            + [f.as_of_time for f in all_facilities]
        )
        if d is not None
    ]
    fallback_as_of = max(as_of_dates) if as_of_dates else date.today()

    if target_job is not None:
        preview: dict[str, Any] = dict(target_job.preview or {})
        raw_cover = preview.get("cover")
        cover_dict: dict[str, Any] = raw_cover if isinstance(raw_cover, dict) else {}
        project_title = (
            str(cover_dict.get("project_title") or "").strip()
            or target_job.original_filename
            or "课题组资料库档案"
        )
        rel_note = str(preview.get("relationship_note") or "").strip() or None
        as_of_date = target_job.as_of_time or fallback_as_of
        doc_key = f"lab-profile-{target_job.id}"
        source = f"import-{target_job.id.hex[:24]}"
    else:
        project_title = "课题组资料库档案"
        rel_note = None
        as_of_date = fallback_as_of
        doc_key = "lab-profile-current"
        source = "lab-profile-current"

    if rel_note is None:
        for proj in all_projects:
            if proj.relationship_note and proj.relationship_note.strip():
                rel_note = proj.relationship_note.strip()
                break

    profile_md = render_lab_profile_markdown(
        project_title=project_title,
        as_of_time=as_of_date,
        people=all_people,
        projects=all_projects,
        achievements=all_achievements,
        facilities=all_facilities,
        relationship_note=rel_note,
    )
    existing_doc = await session.scalar(
        select(Document).where(Document.doc_key == doc_key)
    )
    prev_profile_doc = await session.scalar(
        select(Document)
        .where(
            Document.doc_key.like("lab-profile-%"),
            Document.deleted_at.is_(None),
        )
        .order_by(Document.updated_at.desc(), Document.created_at.desc())
        .limit(1)
    )
    if existing_doc is not None and existing_doc.deleted_at is None:
        visibility = existing_doc.visibility
    elif prev_profile_doc is not None:
        visibility = prev_profile_doc.visibility
    elif existing_doc is not None:
        visibility = existing_doc.visibility
    else:
        visibility = "lab"

    effective_owner_id: uuid.UUID | None = None
    if (
        existing_doc is not None
        and existing_doc.deleted_at is None
        and existing_doc.owner_id is not None
    ):
        effective_owner_id = existing_doc.owner_id
    elif prev_profile_doc is not None and prev_profile_doc.owner_id is not None:
        effective_owner_id = prev_profile_doc.owner_id
    elif existing_doc is not None and existing_doc.owner_id is not None:
        effective_owner_id = existing_doc.owner_id
    elif target_job is not None and (
        target_job.provisioned_by is not None or target_job.uploaded_by is not None
    ):
        candidate_id = target_job.provisioned_by or target_job.uploaded_by
        candidate_user = (
            await session.get(User, candidate_id) if candidate_id is not None else None
        )
        if candidate_user is not None and candidate_user.role == "admin":
            effective_owner_id = candidate_user.id
    if effective_owner_id is None:
        admin_user_id = await session.scalar(
            select(User.id)
            .where(User.role == "admin")
            .order_by(User.created_at.asc())
            .limit(1)
        )
        effective_owner_id = admin_user_id or actor_id

    acl_source_doc = (
        existing_doc
        if (existing_doc is not None and existing_doc.deleted_at is None)
        else (prev_profile_doc or existing_doc)
    )
    preserved_user_grants: list[uuid.UUID] = []
    preserved_group_grants: list[uuid.UUID] = []
    preserved_role_grants: list[str] = []
    if acl_source_doc is not None:
        existing_acl_rows = list(
            await session.scalars(
                select(DocumentACL).where(
                    DocumentACL.document_id == acl_source_doc.id
                )
            )
        )
        for acl_row in existing_acl_rows:
            if acl_row.principal_type == "user":
                try:
                    preserved_user_grants.append(uuid.UUID(acl_row.principal_id))
                except ValueError:
                    pass
            elif acl_row.principal_type == "group":
                try:
                    preserved_group_grants.append(uuid.UUID(acl_row.principal_id))
                except ValueError:
                    pass
            elif acl_row.principal_type == "role" and acl_row.principal_id:
                preserved_role_grants.append(acl_row.principal_id)

    await upsert_source(
        session,
        embedder=embedder,
        owner_id=effective_owner_id,
        source=source,
        records=[
            (
                {
                    "key": doc_key,
                    "title": f"课题组资料库 · {project_title}",
                    "collection": "project",
                    "visibility": visibility,
                    "source_format": "markdown",
                },
                profile_md,
            )
        ],
        grants={doc_key: preserved_user_grants} if preserved_user_grants else None,
        group_grants=(
            {doc_key: preserved_group_grants} if preserved_group_grants else None
        ),
        role_grants=(
            {doc_key: preserved_role_grants} if preserved_role_grants else None
        ),
    )
    rag_doc = await session.scalar(select(Document).where(Document.doc_key == doc_key))
    if rag_doc is not None and rag_doc.owner_id != effective_owner_id:
        rag_doc.owner_id = effective_owner_id
    if target_job is not None and rag_doc is not None:
        target_job.rag_document_id = rag_doc.id

    stale_docs = list(
        await session.scalars(
            select(Document).where(
                Document.doc_key.like("lab-profile-%"),
                Document.doc_key != doc_key,
                Document.deleted_at.is_(None),
            )
        )
    )
    if stale_docs:
        retired_at = datetime.now(UTC)
        for stale_doc in stale_docs:
            await session.execute(
                delete(Chunk).where(Chunk.document_id == stale_doc.id)
            )
            stale_doc.deleted_at = retired_at

    await session.flush()
    return doc_key, rag_doc


async def build_job_roster(
    session: AsyncSession,
    *,
    job: ImportJob,
    settings: Settings,
) -> tuple[str, str]:
    """Re-render password-free text & CSV rosters for a provisioned ImportJob."""
    preview: dict[str, Any] = dict(job.preview or {})
    stats: dict[str, Any] = dict(job.stats or {})
    persisted_rows = stats.get("roster_rows")
    if not isinstance(persisted_rows, list):
        persisted_rows = preview.get("provisioned_roster_rows")

    new_roster_items: list[dict[str, object]] = []
    existing_roster_items: list[dict[str, object]] = []

    if isinstance(persisted_rows, list):
        for raw_row in persisted_rows:
            if not isinstance(raw_row, dict):
                continue
            row_copy = dict(raw_row)
            if str(row_copy.get("account_status") or "new") == "existing":
                existing_roster_items.append(row_copy)
            else:
                new_roster_items.append(row_copy)
    else:
        raw_people_list = preview.get("people")
        raw_people: list[dict[str, Any]] = [
            dict(p)
            for p in (raw_people_list if isinstance(raw_people_list, list) else [])
            if isinstance(p, dict) and not p.get("exclude")
        ]

        users = list(await session.scalars(select(User)))
        by_login = {
            u.login_name.lower(): u
            for u in users
            if u.login_name is not None and u.login_name.strip()
        }
        by_email = {
            u.email.lower(): u
            for u in users
            if u.email is not None and u.email.strip()
        }

        for person in raw_people:
            pname = normalize_person_name(str(person.get("name") or ""))
            p_login = str(person.get("login_name") or "").strip().lower()
            p_email = str(person.get("email") or "").strip().lower() or None
            p_aff = str(person.get("affiliation") or "").strip() or None
            raw_w = person.get("warnings")
            p_warnings = [str(w) for w in (raw_w if isinstance(raw_w, list) else [])]

            matched_user = by_login.get(p_login) if p_login else None
            if matched_user is None and p_email:
                matched_user = by_email.get(p_email)

            effective_login = (
                (matched_user.login_name or matched_user.email or p_login)
                if matched_user is not None
                else p_login
            )
            has_email = (
                (matched_user.email is not None)
                if matched_user is not None
                else (p_email is not None)
            )

            raw_match = person.get("match")
            match_info = raw_match if isinstance(raw_match, dict) else {}
            if str(match_info.get("status") or "new") == "existing":
                existing_roster_items.append(
                    {
                        "name": pname,
                        "login_name": effective_login,
                        "has_email": has_email,
                        "affiliation": p_aff,
                    }
                )
            else:
                new_roster_items.append(
                    {
                        "name": pname,
                        "login_name": effective_login,
                        "has_email": has_email,
                        "suffixed": "LOGIN_NAME_SUFFIXED" in p_warnings,
                        "affiliation": p_aff,
                    }
                )

    oidc_mode = bool(settings.oidc_issuer and settings.oidc_issuer.strip())
    base_time = job.provisioned_at or job.created_at or datetime.now(UTC)
    deadline_utc = base_time + timedelta(days=max(1, settings.initial_password_ttl_days))
    try:
        tz = ZoneInfo(settings.display_tz)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    deadline_display = deadline_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")

    roster_text = render_roster_text(
        base_url=settings.public_base_url,
        initial_password=None,
        include_password=False,
        deadline_str=deadline_display,
        new_accounts=new_roster_items,
        existing_accounts=existing_roster_items,
        oidc_mode=oidc_mode,
        password_notice="（见开号时的通知）",
    )
    roster_csv = render_roster_csv(
        initial_password=None,
        include_password=False,
        deadline_str=deadline_display,
        new_accounts=new_roster_items,
        existing_accounts=existing_roster_items,
        oidc_mode=oidc_mode,
    )
    return roster_text, roster_csv
