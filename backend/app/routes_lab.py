"""API routes for Proposal Cold-Start Import (/api/v1/admin/imports) and Lab Assets Hub (/api/v1/lab)."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import docx
from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.fingerprint import achievement_fp, facility_fp, project_fp
from app.importer.formatters import (
    ACHIEVEMENT_KIND_LABELS,
    PROJECT_STATUS_LABELS,
    format_achievement_application,
    format_facility_application,
    format_person_application,
    format_project_application,
    render_lab_profile_markdown,
)
from app.importer.parser import parse_proposal_docx
from app.importer.people import assign_people_identities, clean_email, normalize_person_name
from app.importer.pii import PIILeakError, assert_no_pii, scrub_free_text
from app.importer.provision import (
    ProvisionError,
    ProvisionRequest,
    _parse_decimal,
    _parse_iso_date,
    build_job_roster,
    provision_import_job,
    sync_lab_profile_rag,
)
from app.importer.reader import DocxValidationError
from app.models import (
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
    User,
)
from app.routes import APIError, _actor, _client_ip, _embedder, _settings, _user
from app.routes_admin import _require_admin

router = APIRouter(prefix="/api/v1")


class UpdateImportPreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of_time: str | date | None = None
    cover: dict[str, Any] | None = None
    people: list[dict[str, Any]] | None = None
    projects: list[dict[str, Any]] | None = None
    achievements: list[dict[str, Any]] | None = None
    facilities: list[dict[str, Any]] | None = None
    relationship_note: str | None = None


class ReparseImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "fsm"


class UpdatePersonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    email: str | None = None
    title: str | None = None
    admin_duty: str | None = None
    tutor_role: str | None = None
    research_specialty: str | None = None
    degree: str | None = None
    affiliation: str | None = None
    project_role: str | None = None
    bio: str | None = None


class ProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    funder: str | None = None
    grant_no: str | None = None
    pi_name: str | None = None
    lab_role: str | None = None
    start_date: str | date | None = None
    end_date: str | date | None = None
    amount_wan: str | float | Decimal | None = None
    status: str | None = None
    status_source: str | None = None
    relationship_note: str | None = None
    raw_text: str | None = None
    as_of_time: str | date | None = None
    source_import_id: uuid.UUID | None = None


class AchievementBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str | None = None
    title: str | None = None
    first_author: str | None = None
    authors_text: str | None = None
    venue: str | None = None
    year: int | None = None
    support_note: str | None = None
    raw_text: str | None = None
    parse_quality: str | None = None
    as_of_time: str | date | None = None
    source_import_id: uuid.UUID | None = None


class FacilityBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str | None = None
    name: str | None = None
    spec: str | None = None
    value_wan: str | float | Decimal | None = None
    usage_scenario: str | None = None
    raw_text: str | None = None
    as_of_time: str | date | None = None
    source_import_id: uuid.UUID | None = None


class BatchFormatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tab: Literal["people", "projects", "achievements", "facilities"]
    ids: list[uuid.UUID] | None = None
    numbered: bool = True
    only_ongoing: bool = False
    recent_years: int | None = None
    with_support: bool = True
    include_bio: bool = False
    include_relationship: bool = False


def _scrub_str(val: str | None) -> str | None:
    if val is None:
        return None
    scrubbed, _ = scrub_free_text(val)
    return scrubbed.strip() or None


def _scrub_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        scrubbed, _ = scrub_free_text(obj)
        return scrubbed
    if isinstance(obj, dict):
        return {k: _scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_obj(item) for item in obj]
    return obj


def _recompute_inferred_projects(
    projects: list[dict[str, Any]], as_of_date: date | None
) -> None:
    for idx, proj in enumerate(projects, start=1):
        proj.setdefault("key", f"j{idx}")
        status_source = str(proj.get("status_source") or "explicit")
        end_d = _parse_iso_date(proj.get("end_date"))
        if status_source == "inferred" and end_d is not None and as_of_date is not None:
            proj["status"] = "completed" if end_d < as_of_date else "ongoing"
        title = str(proj.get("title") or "").strip()
        funder = str(proj.get("funder") or "").strip() or None
        grant_no = str(proj.get("grant_no") or "").strip() or None
        raw_w = proj.get("warnings")
        w_list = [str(w) for w in (raw_w if isinstance(raw_w, list) else [])]
        proj["fingerprint"] = project_fp(
            grant_no=grant_no,
            title=title,
            funder=funder,
            warnings=w_list,
        )


def _serialize_import_job(
    job: ImportJob, *, include_preview: bool = True
) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(job.id),
        "job_id": str(job.id),
        "uploaded_by": str(job.uploaded_by),
        "original_filename": job.original_filename,
        "content_sha256": job.content_sha256,
        "parser_version": job.parser_version,
        "as_of_time": job.as_of_time.isoformat() if job.as_of_time else None,
        "status": job.status,
        "stats": job.stats or {},
        "error_code": job.error_code,
        "rag_document_id": str(job.rag_document_id) if job.rag_document_id else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "provisioned_at": job.provisioned_at.isoformat() if job.provisioned_at else None,
        "provisioned_by": str(job.provisioned_by) if job.provisioned_by else None,
    }
    if include_preview:
        payload["preview"] = job.preview or {}
    return payload


def _serialize_person(person: LabPerson) -> dict[str, object]:
    return {
        "id": str(person.id),
        "user_id": str(person.user_id) if person.user_id else None,
        "name": person.name,
        "email": person.email,
        "title": person.title,
        "admin_duty": person.admin_duty,
        "tutor_role": person.tutor_role,
        "research_specialty": person.research_specialty,
        "degree": person.degree,
        "affiliation": person.affiliation,
        "project_role": person.project_role,
        "bio": person.bio,
        "as_of_time": person.as_of_time.isoformat() if person.as_of_time else None,
        "source_import_id": (
            str(person.source_import_id) if person.source_import_id else None
        ),
        "application_text": format_person_application(person, include_bio=False),
        "application_text_with_bio": format_person_application(person, include_bio=True),
    }


def _serialize_project(project: LabProject) -> dict[str, object]:
    return {
        "id": str(project.id),
        "title": project.title,
        "funder": project.funder,
        "grant_no": project.grant_no,
        "pi_name": project.pi_name,
        "lab_role": project.lab_role,
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "end_date": project.end_date.isoformat() if project.end_date else None,
        "amount_wan": str(project.amount_wan) if project.amount_wan is not None else None,
        "status": project.status,
        "status_label": PROJECT_STATUS_LABELS.get(project.status, "状态未注明"),
        "status_source": project.status_source,
        "relationship_note": project.relationship_note,
        "raw_text": project.raw_text,
        "as_of_time": project.as_of_time.isoformat() if project.as_of_time else None,
        "source_import_id": (
            str(project.source_import_id) if project.source_import_id else None
        ),
        "application_text": format_project_application(
            project, include_relationship=False
        ),
        "application_text_with_rel": format_project_application(
            project, include_relationship=True
        ),
    }


def _serialize_achievement(achievement: LabAchievement) -> dict[str, object]:
    return {
        "id": str(achievement.id),
        "kind": achievement.kind,
        "kind_label": ACHIEVEMENT_KIND_LABELS.get(achievement.kind, "其他成果"),
        "title": achievement.title,
        "first_author": achievement.first_author,
        "authors_text": achievement.authors_text,
        "venue": achievement.venue,
        "year": achievement.year,
        "support_note": achievement.support_note,
        "raw_text": achievement.raw_text,
        "parse_quality": achievement.parse_quality,
        "as_of_time": (
            achievement.as_of_time.isoformat() if achievement.as_of_time else None
        ),
        "source_import_id": (
            str(achievement.source_import_id) if achievement.source_import_id else None
        ),
        "application_text": format_achievement_application(
            achievement, include_support=True
        ),
        "application_text_plain": format_achievement_application(
            achievement, include_support=False
        ),
    }


def _serialize_facility(facility: LabFacility) -> dict[str, object]:
    return {
        "id": str(facility.id),
        "category": facility.category,
        "name": facility.name,
        "spec": facility.spec,
        "value_wan": str(facility.value_wan) if facility.value_wan is not None else None,
        "usage_scenario": facility.usage_scenario,
        "raw_text": facility.raw_text,
        "as_of_time": facility.as_of_time.isoformat() if facility.as_of_time else None,
        "source_import_id": (
            str(facility.source_import_id) if facility.source_import_id else None
        ),
        "application_text": format_facility_application(facility),
    }


@router.post("/admin/imports/proposal")
@router.post("/admin/imports")
async def parse_proposal_upload(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    file: Annotated[UploadFile, File(...)],
    as_of_time: Annotated[str | None, Form()] = None,
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="import_proposal_parse",
        resource_type="import_job",
    )
    settings = _settings(request)
    override_date: date | None = None
    if as_of_time is not None and as_of_time.strip():
        try:
            override_date = date.fromisoformat(as_of_time.strip())
        except ValueError as exc:
            raise APIError(
                422,
                "as_of_time_invalid",
                "基准日期格式无效，请使用 YYYY-MM-DD",
            ) from exc

    raw_bytes = await file.read()
    filename = (file.filename or "proposal.docx").strip()
    existing_users = list(await session.scalars(select(User)))
    existing_people = list(await session.scalars(select(LabPerson)))

    try:
        parsed_bundle: dict[str, Any] = parse_proposal_docx(
            filename,
            raw_bytes,
            existing_users=existing_users,
            existing_people=existing_people,
            as_of_time_override=override_date,
            max_file_mb=settings.import_max_file_mb,
            max_uncompressed_mb=settings.import_max_uncompressed_mb,
        )
    except DocxValidationError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc

    content_sha = str(parsed_bundle.get("content_sha256") or "")
    existing_parsed = await session.scalar(
        select(ImportJob)
        .where(
            ImportJob.content_sha256 == content_sha,
            ImportJob.status == "parsed",
        )
        .order_by(ImportJob.created_at.desc())
        .limit(1)
    )
    if existing_parsed is not None and (
        override_date is None or existing_parsed.as_of_time == override_date
    ):
        return {
            **_serialize_import_job(existing_parsed, include_preview=True),
            "deduplicated": True,
        }

    raw_preview = parsed_bundle.get("preview")
    preview: dict[str, Any] = raw_preview if isinstance(raw_preview, dict) else {}
    raw_stats = parsed_bundle.get("stats")
    bundle_stats: dict[str, Any] = raw_stats if isinstance(raw_stats, dict) else {}

    parsed_as_of = parsed_bundle.get("as_of_time")
    if not isinstance(parsed_as_of, date):
        parsed_as_of = None

    raw_people = preview.get("people")
    people_list: list[Any] = raw_people if isinstance(raw_people, list) else []
    raw_ach = preview.get("achievements")
    ach_list: list[Any] = raw_ach if isinstance(raw_ach, list) else []
    raw_proj = preview.get("projects")
    proj_list: list[Any] = raw_proj if isinstance(raw_proj, list) else []
    raw_fac = preview.get("facilities")
    fac_list: list[Any] = raw_fac if isinstance(raw_fac, list) else []
    pii_dropped = int(bundle_stats.get("pii_dropped") or 0)

    stats: dict[str, object] = {
        "people_parsed": len(people_list),
        "achievements_parsed": len(ach_list),
        "projects_parsed": len(proj_list),
        "facilities_parsed": len(fac_list),
        "pii_dropped": pii_dropped,
    }

    job = ImportJob(
        uploaded_by=actor.id,
        original_filename=filename[:255],
        content_sha256=content_sha,
        parser_version=str(parsed_bundle.get("parser_version") or "v2.1-hybrid"),
        as_of_time=parsed_as_of,
        status="parsed",
        preview=preview,
        stats=stats,
    )
    session.add(job)
    await session.flush()
    serialized = _serialize_import_job(job, include_preview=True)
    await session.commit()

    await write_audit(
        settings=settings,
        actor_id=actor.id,
        action="import_proposal_parse",
        resource_type="import_job",
        resource_id=str(job.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "filename": filename[:120],
            "people": len(people_list),
            "projects": len(proj_list),
            "achievements": len(ach_list),
            "facilities": len(fac_list),
            "pii_dropped": pii_dropped,
        },
    )
    return {**serialized, "deduplicated": False}


@router.get("/admin/imports")
async def list_import_jobs(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="list_imports",
        resource_type="import_job",
    )
    rows = list(
        await session.scalars(
            select(ImportJob).order_by(ImportJob.created_at.desc()).limit(50)
        )
    )
    return {
        "imports": [_serialize_import_job(job, include_preview=False) for job in rows]
    }


@router.get("/admin/imports/{job_id}")
@router.get("/admin/imports/{job_id}/preview")
async def get_import_job(
    job_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="get_import",
        resource_type="import_job",
        resource_id=str(job_id),
    )
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise APIError(404, "not_found", "导入任务不存在")
    return _serialize_import_job(job, include_preview=True)


@router.delete("/admin/imports/{job_id}")
async def delete_import_job(
    job_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="delete_import",
        resource_type="import_job",
        resource_id=str(job_id),
    )
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise APIError(404, "not_found", "导入任务不存在")
    if job.status in ("provisioned", "merged") or job.status not in ("parsed", "failed"):
        raise APIError(
            409,
            "import_already_provisioned",
            "已开号或已合并的导入任务不可删除",
        )
    filename = job.original_filename
    await session.delete(job)
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="import_job_deleted",
        resource_type="import_job",
        resource_id=str(job_id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"original_filename": filename},
    )
    return {"status": "deleted", "id": str(job_id), "job_id": str(job_id)}


@router.get("/admin/imports/{job_id}/roster")
async def get_import_job_roster(
    job_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    format: str = Query(default="text"),
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="get_import_roster",
        resource_type="import_job",
        resource_id=str(job_id),
    )
    fmt = format.strip().lower()
    if fmt not in ("text", "csv"):
        raise APIError(400, "invalid_format", "format 仅支持 text 或 csv")

    job = await session.get(ImportJob, job_id)
    if job is None:
        raise APIError(404, "not_found", "导入任务不存在")
    if job.status not in ("provisioned", "merged"):
        raise APIError(409, "import_not_provisioned", "该导入任务尚未完成开号")

    roster_text, roster_csv = await build_job_roster(
        session,
        job=job,
        settings=_settings(request),
    )
    return {
        "job_id": str(job.id),
        "format": fmt,
        "roster_text": roster_text,
        "roster_csv": roster_csv,
    }


@router.patch("/admin/imports/{job_id}/preview")
async def update_import_preview(
    job_id: uuid.UUID,
    body: UpdateImportPreviewBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="update_import_preview",
        resource_type="import_job",
        resource_id=str(job_id),
    )
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise APIError(404, "not_found", "导入任务不存在")
    if job.status != "parsed":
        raise APIError(
            409,
            "import_already_provisioned",
            "该导入任务已开号，不可修改预览",
        )

    preview: dict[str, Any] = dict(job.preview or {})
    recompute_projects = False
    recompute_people = False

    if body.as_of_time is not None:
        parsed_d = _parse_iso_date(body.as_of_time)
        if parsed_d is None:
            raise APIError(
                422,
                "as_of_time_invalid",
                "基准日期格式无效，请使用 YYYY-MM-DD",
            )
        job.as_of_time = parsed_d
        preview["as_of_time"] = parsed_d.isoformat()
        recompute_projects = True

    if body.cover is not None:
        cur_cover = dict(preview.get("cover") or {}) if isinstance(preview.get("cover"), dict) else {}
        cur_cover.update(_scrub_obj(body.cover))
        preview["cover"] = cur_cover

    if body.relationship_note is not None:
        preview["relationship_note"] = _scrub_str(body.relationship_note)

    if body.people is not None:
        preview["people"] = [_scrub_obj(dict(p)) for p in body.people]
        recompute_people = True

    if body.projects is not None:
        preview["projects"] = [_scrub_obj(dict(j)) for j in body.projects]
        recompute_projects = True

    if body.achievements is not None:
        ach_list = [_scrub_obj(dict(a)) for a in body.achievements]
        for idx, ach in enumerate(ach_list, start=1):
            ach.setdefault("key", f"a{idx}")
            kind = str(ach.get("kind") or "other")
            title = str(ach.get("title") or "").strip()
            raw_t = str(ach.get("raw_text") or title).strip()
            yr = int(ach["year"]) if isinstance(ach.get("year"), int) else None
            pq = str(ach.get("parse_quality") or "structured")
            ach["fingerprint"] = achievement_fp(
                kind=kind, title=title, year=yr, raw_text=raw_t, parse_quality=pq
            )
        preview["achievements"] = ach_list

    if body.facilities is not None:
        fac_list = [_scrub_obj(dict(f)) for f in body.facilities]
        for idx, fac in enumerate(fac_list, start=1):
            fac.setdefault("key", f"f{idx}")
            cat = str(fac.get("category") or "equipment")
            fname = str(fac.get("name") or "").strip()
            fspec = str(fac.get("spec") or "").strip() or None
            fac["fingerprint"] = facility_fp(category=cat, name=fname, spec=fspec)
        preview["facilities"] = fac_list

    if recompute_people:
        existing_users = list(await session.scalars(select(User)))
        existing_people = list(await session.scalars(select(LabPerson)))
        raw_ppl = preview.get("people")
        ppl_list: list[dict[str, Any]] = [
            dict(p) for p in (raw_ppl if isinstance(raw_ppl, list) else []) if isinstance(p, dict)
        ]
        for p in ppl_list:
            if p.get("tier") == "T1" and not p.get("cert"):
                p["cert"] = p.get("login_name")
        assign_people_identities(
            ppl_list,
            existing_users=existing_users,
            existing_people=existing_people,
        )
        preview["people"] = ppl_list

    if recompute_projects:
        raw_projs = preview.get("projects")
        proj_list: list[dict[str, Any]] = [
            dict(j) for j in (raw_projs if isinstance(raw_projs, list) else []) if isinstance(j, dict)
        ]
        _recompute_inferred_projects(proj_list, job.as_of_time)
        preview["projects"] = proj_list

    try:
        assert_no_pii(preview)
    except PIILeakError as exc:
        raise APIError(422, "pii_detected", str(exc)) from exc

    cur_stats = dict(job.stats or {})
    cur_stats["people_parsed"] = len(preview.get("people") or [])
    cur_stats["projects_parsed"] = len(preview.get("projects") or [])
    cur_stats["achievements_parsed"] = len(preview.get("achievements") or [])
    cur_stats["facilities_parsed"] = len(preview.get("facilities") or [])

    job.preview = preview
    job.stats = cur_stats
    await session.flush()
    serialized = _serialize_import_job(job, include_preview=True)
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="import_preview_updated",
        resource_type="import_job",
        resource_id=str(job.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "people_parsed": cur_stats["people_parsed"],
            "projects_parsed": cur_stats["projects_parsed"],
            "achievements_parsed": cur_stats["achievements_parsed"],
            "facilities_parsed": cur_stats["facilities_parsed"],
        },
    )
    return serialized


@router.post("/admin/imports/{job_id}/reparse")
async def reparse_import_job(
    job_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    mode: str | None = Query(default=None),
    body: ReparseImportBody | None = Body(default=None),
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="reparse_import",
        resource_type="import_job",
        resource_id=str(job_id),
    )
    effective_mode = (mode or (body.mode if body is not None else None) or "fsm").strip().lower()
    if effective_mode not in ("llm", "fsm"):
        raise APIError(400, "invalid_mode", "mode 仅支持 llm 或 fsm")

    job = await session.get(ImportJob, job_id)
    if job is None:
        raise APIError(404, "not_found", "导入任务不存在")
    if job.status != "parsed":
        raise APIError(
            409,
            "import_already_provisioned",
            "该导入任务已开号，不可重新解析",
        )

    preview: dict[str, Any] = _scrub_obj(dict(job.preview or {}))
    existing_users = list(await session.scalars(select(User)))
    existing_people = list(await session.scalars(select(LabPerson)))

    raw_ppl = preview.get("people")
    ppl_list: list[dict[str, Any]] = [
        dict(p) for p in (raw_ppl if isinstance(raw_ppl, list) else []) if isinstance(p, dict)
    ]
    for p in ppl_list:
        if p.get("tier") == "T1" and not p.get("cert"):
            p["cert"] = p.get("login_name")
    assign_people_identities(
        ppl_list,
        existing_users=existing_users,
        existing_people=existing_people,
    )
    preview["people"] = ppl_list

    raw_projs = preview.get("projects")
    proj_list: list[dict[str, Any]] = [
        dict(j) for j in (raw_projs if isinstance(raw_projs, list) else []) if isinstance(j, dict)
    ]
    _recompute_inferred_projects(proj_list, job.as_of_time)
    preview["projects"] = proj_list

    parser_ver = "v2.1-llm" if effective_mode == "llm" else "v2.1-fsm"
    ext_mode = "rule+llm" if effective_mode == "llm" else "rule+regex_fsm"
    job.parser_version = parser_ver
    preview["parser_version"] = parser_ver
    preview["extraction_mode"] = ext_mode

    assert_no_pii(preview)
    job.preview = preview
    await session.flush()
    serialized = _serialize_import_job(job, include_preview=True)
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="import_job_reparsed",
        resource_type="import_job",
        resource_id=str(job.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"mode": effective_mode, "parser_version": parser_ver},
    )
    return serialized


@router.post("/admin/imports/{job_id}/provision")
async def provision_import(
    job_id: uuid.UUID,
    body: ProvisionRequest,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="import_proposal_provision",
        resource_type="import_job",
        resource_id=str(job_id),
    )
    settings = _settings(request)
    embedder = _embedder(request)
    try:
        result = await provision_import_job(
            session,
            job_id=job_id,
            actor_id=actor.id,
            body=body,
            settings=settings,
            embedder=embedder,
        )
    except ProvisionError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc

    await session.commit()
    raw_res_stats = result.get("stats")
    stats: dict[str, Any] = raw_res_stats if isinstance(raw_res_stats, dict) else {}
    await write_audit(
        settings=settings,
        actor_id=actor.id,
        action="import_proposal_provision",
        resource_type="import_job",
        resource_id=str(job_id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "users_created": int(stats.get("users_created") or 0),
            "users_linked": int(stats.get("users_linked") or 0),
            "people_inserted": int(stats.get("people_inserted") or 0),
            "achievements_inserted": int(stats.get("achievements_inserted") or 0),
            "projects_inserted": int(stats.get("projects_inserted") or 0),
            "facilities_inserted": int(stats.get("facilities_inserted") or 0),
            "include_password": bool(body.include_password),
        },
    )
    return result


@router.get("/lab/overview")
async def lab_overview(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _user(session, actor)
    people = list(
        await session.scalars(
            select(LabPerson).order_by(
                LabPerson.project_role.asc(), LabPerson.created_at.asc()
            )
        )
    )
    projects = list(
        await session.scalars(select(LabProject).order_by(LabProject.created_at.asc()))
    )
    achievements = list(
        await session.scalars(
            select(LabAchievement).order_by(LabAchievement.created_at.asc())
        )
    )
    facilities = list(
        await session.scalars(
            select(LabFacility).order_by(LabFacility.created_at.asc())
        )
    )
    latest_job = await session.scalar(
        select(ImportJob)
        .where(ImportJob.status == "provisioned")
        .order_by(ImportJob.provisioned_at.desc())
        .limit(1)
    )

    ongoing_count = sum(1 for p in projects if p.status == "ongoing")
    completed_count = sum(1 for p in projects if p.status == "completed")
    as_of_dates = [
        d
        for d in (
            [p.as_of_time for p in people]
            + [j.as_of_time for j in projects]
            + [a.as_of_time for a in achievements]
            + [f.as_of_time for f in facilities]
        )
        if d is not None
    ]
    latest_as_of = max(as_of_dates).isoformat() if as_of_dates else None

    return {
        "counts": {
            "people": len(people),
            "projects": len(projects),
            "projects_ongoing": ongoing_count,
            "projects_completed": completed_count,
            "achievements": len(achievements),
            "facilities": len(facilities),
        },
        "as_of_time": latest_as_of,
        "latest_import": (
            _serialize_import_job(latest_job, include_preview=False)
            if latest_job is not None
            else None
        ),
    }


@router.get("/lab/people")
async def list_lab_people(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    role: str | None = Query(default=None),
) -> dict[str, object]:
    await _user(session, actor)
    rows = list(
        await session.scalars(
            select(LabPerson).order_by(
                LabPerson.project_role.asc(), LabPerson.created_at.asc()
            )
        )
    )
    if role and role.strip():
        r_clean = role.strip().lower()
        rows = [p for p in rows if p.project_role == r_clean]
    if q and q.strip():
        needle = q.strip().lower()
        rows = [
            p
            for p in rows
            if needle in (p.name or "").lower()
            or needle in (p.research_specialty or "").lower()
            or needle in (p.title or "").lower()
            or needle in (p.affiliation or "").lower()
            or needle in (p.bio or "").lower()
        ]
    return {"people": [_serialize_person(p) for p in rows]}


@router.get("/lab/projects")
async def list_lab_projects(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    role: str | None = Query(default=None),
) -> dict[str, object]:
    await _user(session, actor)
    rows = list(
        await session.scalars(select(LabProject).order_by(LabProject.created_at.asc()))
    )
    if status and status.strip():
        st_clean = status.strip().lower()
        rows = [p for p in rows if p.status == st_clean]
    if role and role.strip():
        rl_clean = role.strip().lower()
        rows = [p for p in rows if p.lab_role == rl_clean]
    if q and q.strip():
        needle = q.strip().lower()
        rows = [
            p
            for p in rows
            if needle in (p.title or "").lower()
            or needle in (p.grant_no or "").lower()
            or needle in (p.funder or "").lower()
            or needle in (p.pi_name or "").lower()
            or needle in (p.relationship_note or "").lower()
        ]
    return {"projects": [_serialize_project(p) for p in rows]}


@router.get("/lab/achievements")
async def list_lab_achievements(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    year: int | None = Query(default=None),
) -> dict[str, object]:
    await _user(session, actor)
    rows = list(
        await session.scalars(
            select(LabAchievement).order_by(LabAchievement.created_at.asc())
        )
    )
    if kind and kind.strip():
        k_clean = kind.strip().lower()
        rows = [a for a in rows if a.kind == k_clean]
    if year is not None:
        rows = [a for a in rows if a.year == year]
    if q and q.strip():
        needle = q.strip().lower()
        rows = [
            a
            for a in rows
            if needle in (a.title or "").lower()
            or needle in (a.first_author or "").lower()
            or needle in (a.venue or "").lower()
            or needle in (a.support_note or "").lower()
            or needle in (a.raw_text or "").lower()
        ]
    return {"achievements": [_serialize_achievement(a) for a in rows]}


@router.get("/lab/facilities")
async def list_lab_facilities(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
) -> dict[str, object]:
    await _user(session, actor)
    rows = list(
        await session.scalars(
            select(LabFacility).order_by(LabFacility.created_at.asc())
        )
    )
    if category and category.strip():
        cat_clean = category.strip().lower()
        rows = [f for f in rows if f.category == cat_clean]
    if q and q.strip():
        needle = q.strip().lower()
        rows = [
            f
            for f in rows
            if needle in (f.name or "").lower()
            or needle in (f.spec or "").lower()
            or needle in (f.usage_scenario or "").lower()
            or needle in (f.raw_text or "").lower()
        ]
    return {"facilities": [_serialize_facility(f) for f in rows]}


async def _resolve_source_import_id(
    session: AsyncSession,
    actor_id: uuid.UUID,
    as_of_date: date,
    explicit_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, date]:
    if explicit_id is not None:
        job = await session.get(ImportJob, explicit_id)
        if job is not None:
            return job.id, job.as_of_time or as_of_date
    latest_job = await session.scalar(
        select(ImportJob)
        .order_by(
            (ImportJob.status == "provisioned").desc(),
            ImportJob.created_at.desc(),
        )
        .limit(1)
    )
    if latest_job is not None:
        return latest_job.id, latest_job.as_of_time or as_of_date
    placeholder = ImportJob(
        uploaded_by=actor_id,
        original_filename="manual-entry.docx",
        content_sha256="0" * 64,
        parser_version="v2.1-manual",
        as_of_time=as_of_date,
        status="provisioned",
        preview={"cover": {"project_title": "课题组资料库档案"}},
        stats={},
        provisioned_at=datetime.now(UTC),
        provisioned_by=actor_id,
    )
    session.add(placeholder)
    await session.flush()
    return placeholder.id, as_of_date


async def _require_lab_editor(
    session: AsyncSession,
    actor: Actor,
    *,
    owner_name: str | None = None,
) -> User:
    user = await _user(session, actor)
    if actor.role == "admin":
        return user
    if actor.role != "member":
        raise APIError(403, "forbidden", "权限不足")
    if owner_name is not None:
        person = await session.scalar(
            select(LabPerson).where(LabPerson.user_id == user.id)
        )
        allowed_names = {
            normalize_person_name(user.display_name),
            normalize_person_name(person.name) if person else "",
        }
        if normalize_person_name(owner_name) not in allowed_names:
            raise APIError(403, "forbidden", "仅管理员或条目负责人可修改此记录")
    return user


@router.post("/lab/reindex")
async def reindex_lab_profile(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    user = await _require_lab_editor(session, actor)
    doc_key, rag_doc = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {
        "status": "ok",
        "rag_doc_key": doc_key,
        "rag_document_id": str(rag_doc.id) if rag_doc is not None else None,
    }


@router.patch("/lab/people/{person_id}")
async def update_lab_person(
    person_id: uuid.UUID,
    body: UpdatePersonBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    user = await _user(session, actor)
    person = await session.get(LabPerson, person_id)
    if person is None:
        raise APIError(404, "not_found", "成员档案不存在")
    if actor.role != "admin" and person.user_id != user.id:
        raise APIError(403, "forbidden", "仅管理员或本人可修改成员档案")

    if body.name is not None:
        cleaned_name = normalize_person_name(_scrub_str(body.name) or "")
        if not cleaned_name:
            raise APIError(422, "invalid_name", "姓名不能为空")
        person.name = cleaned_name
    if body.email is not None:
        person.email = clean_email(body.email)
    if body.title is not None:
        person.title = _scrub_str(body.title)
    if body.admin_duty is not None:
        person.admin_duty = _scrub_str(body.admin_duty)
    if body.tutor_role is not None:
        person.tutor_role = _scrub_str(body.tutor_role)
    if body.research_specialty is not None:
        person.research_specialty = _scrub_str(body.research_specialty)
    if body.degree is not None:
        person.degree = _scrub_str(body.degree)
    if body.affiliation is not None:
        person.affiliation = _scrub_str(body.affiliation)
    if body.project_role is not None:
        pr = body.project_role.strip().lower()
        if pr not in ("applicant", "member"):
            raise APIError(422, "invalid_project_role", "project_role 仅支持 applicant 或 member")
        person.project_role = pr
    if body.bio is not None:
        person.bio = _scrub_str(body.bio)

    serialized_person = _serialize_person(person)
    assert_no_pii(serialized_person)
    await session.flush()

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_person_updated",
        resource_type="lab_person",
        resource_id=str(person.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"rag_doc_key": doc_key},
    )
    return {"person": _serialize_person(person), "rag_doc_key": doc_key}


@router.post("/lab/projects")
async def create_lab_project(
    body: ProjectBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    user = await _require_lab_editor(session, actor)
    title = _scrub_str(body.title)
    if not title:
        raise APIError(422, "invalid_title", "项目名称不能为空")

    funder = _scrub_str(body.funder)
    grant_no = _scrub_str(body.grant_no)
    pi_name = _scrub_str(body.pi_name)
    lab_role = (body.lab_role or "host").strip().lower()
    if lab_role not in ("host", "participant", "unknown"):
        lab_role = "host"
    start_d = _parse_iso_date(body.start_date)
    end_d = _parse_iso_date(body.end_date)
    amt_wan = _parse_decimal(body.amount_wan)
    status = (body.status or "ongoing").strip().lower()
    if status not in ("ongoing", "completed", "unknown"):
        status = "unknown"
    status_source = (body.status_source or "explicit").strip().lower()
    if status_source not in ("explicit", "inferred"):
        status_source = "explicit"
    rel_note = _scrub_str(body.relationship_note)
    raw_text = _scrub_str(body.raw_text) or title
    req_as_of = _parse_iso_date(body.as_of_time) or date.today()

    source_import_id, resolved_as_of = await _resolve_source_import_id(
        session,
        user.id,
        req_as_of,
        body.source_import_id,
    )
    as_of_date = _parse_iso_date(body.as_of_time) or resolved_as_of
    fp_val = project_fp(grant_no=grant_no, title=title, funder=funder)
    existing_fp = await session.scalar(
        select(LabProject.id).where(LabProject.entity_fingerprint == fp_val)
    )
    if existing_fp is not None:
        fp_val = project_fp(
            grant_no=f"{grant_no or ''}-{uuid.uuid4().hex[:8]}",
            title=title,
            funder=funder,
        )

    project = LabProject(
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
        relationship_note=rel_note,
        raw_text=raw_text,
        entity_fingerprint=fp_val,
        as_of_time=as_of_date,
        source_import_id=source_import_id,
    )
    session.add(project)
    await session.flush()
    assert_no_pii(_serialize_project(project))

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"project": _serialize_project(project), "rag_doc_key": doc_key}


@router.patch("/lab/projects/{project_id}")
async def update_lab_project(
    project_id: uuid.UUID,
    body: ProjectBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    project = await session.get(LabProject, project_id)
    if project is None:
        raise APIError(404, "not_found", "项目记录不存在")
    user = await _require_lab_editor(session, actor, owner_name=project.pi_name)

    if body.title is not None:
        t_clean = _scrub_str(body.title)
        if not t_clean:
            raise APIError(422, "invalid_title", "项目名称不能为空")
        project.title = t_clean
    if body.funder is not None:
        project.funder = _scrub_str(body.funder)
    if body.grant_no is not None:
        project.grant_no = _scrub_str(body.grant_no)
    if body.pi_name is not None:
        project.pi_name = _scrub_str(body.pi_name)
    if body.lab_role is not None:
        lr = body.lab_role.strip().lower()
        if lr in ("host", "participant", "unknown"):
            project.lab_role = lr
    if body.start_date is not None:
        project.start_date = _parse_iso_date(body.start_date)
    if body.end_date is not None:
        project.end_date = _parse_iso_date(body.end_date)
    if body.amount_wan is not None:
        project.amount_wan = _parse_decimal(body.amount_wan)
    if body.status is not None:
        st = body.status.strip().lower()
        if st in ("ongoing", "completed", "unknown"):
            project.status = st
    if body.status_source is not None:
        ss = body.status_source.strip().lower()
        if ss in ("explicit", "inferred"):
            project.status_source = ss
    if body.relationship_note is not None:
        project.relationship_note = _scrub_str(body.relationship_note)
    if body.raw_text is not None:
        project.raw_text = _scrub_str(body.raw_text) or project.title

    await session.flush()
    assert_no_pii(_serialize_project(project))

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"project": _serialize_project(project), "rag_doc_key": doc_key}


@router.delete("/lab/projects/{project_id}")
async def delete_lab_project(
    project_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    project = await session.get(LabProject, project_id)
    if project is None:
        raise APIError(404, "not_found", "项目记录不存在")
    user = await _require_lab_editor(session, actor, owner_name=project.pi_name)

    await session.delete(project)
    await session.flush()
    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"status": "deleted", "id": str(project_id), "rag_doc_key": doc_key}


@router.post("/lab/achievements")
async def create_lab_achievement(
    body: AchievementBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    user = await _require_lab_editor(session, actor)
    title = _scrub_str(body.title)
    if not title:
        raise APIError(422, "invalid_title", "成果名称不能为空")

    kind = (body.kind or "paper").strip().lower()
    if kind not in ACHIEVEMENT_KIND_LABELS:
        kind = "other"
    first_author = _scrub_str(body.first_author)
    authors_text = _scrub_str(body.authors_text) or first_author
    venue = _scrub_str(body.venue)
    support_note = _scrub_str(body.support_note)
    raw_text = _scrub_str(body.raw_text) or title
    pq = (body.parse_quality or "structured").strip().lower()
    if pq not in ("structured", "raw"):
        pq = "structured"
    req_as_of = _parse_iso_date(body.as_of_time) or date.today()

    source_import_id, resolved_as_of = await _resolve_source_import_id(
        session,
        user.id,
        req_as_of,
        body.source_import_id,
    )
    as_of_date = _parse_iso_date(body.as_of_time) or resolved_as_of
    fp_val = achievement_fp(
        kind=kind, title=title, year=body.year, raw_text=raw_text, parse_quality=pq
    )
    existing_fp = await session.scalar(
        select(LabAchievement.id).where(LabAchievement.entity_fingerprint == fp_val)
    )
    if existing_fp is not None:
        fp_val = achievement_fp(
            kind=kind,
            title=f"{title}-{uuid.uuid4().hex[:8]}",
            year=body.year,
            raw_text=raw_text,
            parse_quality=pq,
        )

    achievement = LabAchievement(
        kind=kind,
        title=title,
        first_author=first_author,
        authors_text=authors_text,
        venue=venue,
        year=body.year,
        support_note=support_note,
        raw_text=raw_text,
        parse_quality=pq,
        entity_fingerprint=fp_val,
        as_of_time=as_of_date,
        source_import_id=source_import_id,
    )
    session.add(achievement)
    await session.flush()
    assert_no_pii(_serialize_achievement(achievement))

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"achievement": _serialize_achievement(achievement), "rag_doc_key": doc_key}


@router.patch("/lab/achievements/{achievement_id}")
async def update_lab_achievement(
    achievement_id: uuid.UUID,
    body: AchievementBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    achievement = await session.get(LabAchievement, achievement_id)
    if achievement is None:
        raise APIError(404, "not_found", "成果记录不存在")
    user = await _require_lab_editor(
        session, actor, owner_name=achievement.first_author
    )

    if body.kind is not None:
        k_clean = body.kind.strip().lower()
        if k_clean in ACHIEVEMENT_KIND_LABELS:
            achievement.kind = k_clean
    if body.title is not None:
        t_clean = _scrub_str(body.title)
        if not t_clean:
            raise APIError(422, "invalid_title", "成果名称不能为空")
        achievement.title = t_clean
    if body.first_author is not None:
        achievement.first_author = _scrub_str(body.first_author)
    if body.authors_text is not None:
        achievement.authors_text = _scrub_str(body.authors_text)
    if body.venue is not None:
        achievement.venue = _scrub_str(body.venue)
    if body.year is not None:
        achievement.year = body.year
    if body.support_note is not None:
        achievement.support_note = _scrub_str(body.support_note)
    if body.raw_text is not None:
        achievement.raw_text = _scrub_str(body.raw_text) or achievement.title

    await session.flush()
    assert_no_pii(_serialize_achievement(achievement))

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"achievement": _serialize_achievement(achievement), "rag_doc_key": doc_key}


@router.delete("/lab/achievements/{achievement_id}")
async def delete_lab_achievement(
    achievement_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    achievement = await session.get(LabAchievement, achievement_id)
    if achievement is None:
        raise APIError(404, "not_found", "成果记录不存在")
    user = await _require_lab_editor(
        session, actor, owner_name=achievement.first_author
    )

    await session.delete(achievement)
    await session.flush()
    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"status": "deleted", "id": str(achievement_id), "rag_doc_key": doc_key}


@router.post("/lab/facilities")
async def create_lab_facility(
    body: FacilityBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    user = await _require_lab_editor(session, actor)
    name = _scrub_str(body.name)
    if not name:
        raise APIError(422, "invalid_name", "设备名称不能为空")

    category = _scrub_str(body.category) or "equipment"
    spec = _scrub_str(body.spec)
    val_wan = _parse_decimal(body.value_wan)
    usage = _scrub_str(body.usage_scenario)
    raw_text = _scrub_str(body.raw_text) or name
    req_as_of = _parse_iso_date(body.as_of_time) or date.today()

    source_import_id, resolved_as_of = await _resolve_source_import_id(
        session,
        user.id,
        req_as_of,
        body.source_import_id,
    )
    as_of_date = _parse_iso_date(body.as_of_time) or resolved_as_of
    fp_val = facility_fp(category=category, name=name, spec=spec)
    existing_fp = await session.scalar(
        select(LabFacility.id).where(LabFacility.entity_fingerprint == fp_val)
    )
    if existing_fp is not None:
        fp_val = facility_fp(
            category=category, name=f"{name}-{uuid.uuid4().hex[:8]}", spec=spec
        )

    facility = LabFacility(
        category=category,
        name=name,
        spec=spec,
        value_wan=val_wan,
        usage_scenario=usage,
        raw_text=raw_text,
        entity_fingerprint=fp_val,
        as_of_time=as_of_date,
        source_import_id=source_import_id,
    )
    session.add(facility)
    await session.flush()
    assert_no_pii(_serialize_facility(facility))

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"facility": _serialize_facility(facility), "rag_doc_key": doc_key}


@router.patch("/lab/facilities/{facility_id}")
async def update_lab_facility(
    facility_id: uuid.UUID,
    body: FacilityBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    facility = await session.get(LabFacility, facility_id)
    if facility is None:
        raise APIError(404, "not_found", "设备记录不存在")
    user = await _require_lab_editor(session, actor)

    if body.category is not None:
        facility.category = _scrub_str(body.category) or facility.category
    if body.name is not None:
        n_clean = _scrub_str(body.name)
        if not n_clean:
            raise APIError(422, "invalid_name", "设备名称不能为空")
        facility.name = n_clean
    if body.spec is not None:
        facility.spec = _scrub_str(body.spec)
    if body.value_wan is not None:
        facility.value_wan = _parse_decimal(body.value_wan)
    if body.usage_scenario is not None:
        facility.usage_scenario = _scrub_str(body.usage_scenario)
    if body.raw_text is not None:
        facility.raw_text = _scrub_str(body.raw_text) or facility.name

    await session.flush()
    assert_no_pii(_serialize_facility(facility))

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"facility": _serialize_facility(facility), "rag_doc_key": doc_key}


@router.delete("/lab/facilities/{facility_id}")
async def delete_lab_facility(
    facility_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    facility = await session.get(LabFacility, facility_id)
    if facility is None:
        raise APIError(404, "not_found", "设备记录不存在")
    user = await _require_lab_editor(session, actor)

    await session.delete(facility)
    await session.flush()
    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"status": "deleted", "id": str(facility_id), "rag_doc_key": doc_key}


@router.post("/lab/format/batch")
async def batch_format_lab_assets(
    body: BatchFormatBody,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _user(session, actor)
    id_filter = set(body.ids) if body.ids is not None else None
    raw_lines: list[str] = []

    if body.tab == "people":
        people = list(
            await session.scalars(
                select(LabPerson).order_by(
                    LabPerson.project_role.asc(), LabPerson.created_at.asc()
                )
            )
        )
        if id_filter is not None:
            people = [p for p in people if p.id in id_filter]
        raw_lines = [
            format_person_application(p, include_bio=body.include_bio) for p in people
        ]
    elif body.tab == "projects":
        projects = list(
            await session.scalars(
                select(LabProject).order_by(LabProject.created_at.asc())
            )
        )
        ref_dates = [p.as_of_time for p in projects if p.as_of_time is not None]
        ref_year = max(ref_dates).year if ref_dates else date.today().year
        if id_filter is not None:
            projects = [p for p in projects if p.id in id_filter]
        if body.only_ongoing:
            projects = [p for p in projects if p.status == "ongoing"]
        if body.recent_years is not None and body.recent_years > 0:
            cutoff_year = ref_year - body.recent_years
            projects = [
                p
                for p in projects
                if p.start_date is not None and p.start_date.year >= cutoff_year
            ]
        raw_lines = [
            format_project_application(p, include_relationship=body.include_relationship)
            for p in projects
        ]
    elif body.tab == "achievements":
        achievements = list(
            await session.scalars(
                select(LabAchievement).order_by(LabAchievement.created_at.asc())
            )
        )
        ref_dates = [a.as_of_time for a in achievements if a.as_of_time is not None]
        ref_year = max(ref_dates).year if ref_dates else date.today().year
        if id_filter is not None:
            achievements = [a for a in achievements if a.id in id_filter]
        if body.recent_years is not None and body.recent_years > 0:
            cutoff_year = ref_year - body.recent_years
            achievements = [
                a
                for a in achievements
                if a.year is not None and a.year >= cutoff_year
            ]
        raw_lines = [
            format_achievement_application(a, include_support=body.with_support)
            for a in achievements
        ]
    elif body.tab == "facilities":
        facilities = list(
            await session.scalars(
                select(LabFacility).order_by(LabFacility.created_at.asc())
            )
        )
        if id_filter is not None:
            facilities = [f for f in facilities if f.id in id_filter]
        raw_lines = [format_facility_application(f) for f in facilities]

    if body.numbered:
        items = [f"[{idx}] {line}" for idx, line in enumerate(raw_lines, start=1)]
    else:
        items = list(raw_lines)
    text_out = "\n".join(items)
    assert_no_pii(text_out)
    return {
        "tab": body.tab,
        "count": len(items),
        "items": items,
        "text": text_out,
    }


@router.get("/lab/export")
async def export_lab_assets(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    format: str = Query(default="md"),
) -> Response:
    await _user(session, actor)
    fmt = format.strip().lower()
    if fmt not in ("md", "docx"):
        raise APIError(400, "invalid_format", "导出格式仅支持 md 或 docx")

    people = list(
        await session.scalars(
            select(LabPerson).order_by(
                LabPerson.project_role.asc(), LabPerson.created_at.asc()
            )
        )
    )
    projects = list(
        await session.scalars(select(LabProject).order_by(LabProject.created_at.asc()))
    )
    achievements = list(
        await session.scalars(
            select(LabAchievement).order_by(LabAchievement.created_at.asc())
        )
    )
    facilities = list(
        await session.scalars(
            select(LabFacility).order_by(LabFacility.created_at.asc())
        )
    )
    latest_job = await session.scalar(
        select(ImportJob)
        .where(ImportJob.status == "provisioned")
        .order_by(ImportJob.provisioned_at.desc(), ImportJob.created_at.desc())
        .limit(1)
    )

    as_of_dates = [
        d
        for d in (
            [p.as_of_time for p in people]
            + [j.as_of_time for j in projects]
            + [a.as_of_time for a in achievements]
            + [f.as_of_time for f in facilities]
        )
        if d is not None
    ]
    as_of_date = (
        (latest_job.as_of_time if latest_job and latest_job.as_of_time else None)
        or (max(as_of_dates) if as_of_dates else date.today())
    )
    project_title = "课题组资料库档案"
    rel_note: str | None = None
    if latest_job is not None:
        preview = dict(latest_job.preview or {})
        raw_cover = preview.get("cover")
        cover_dict = raw_cover if isinstance(raw_cover, dict) else {}
        project_title = (
            str(cover_dict.get("project_title") or "").strip()
            or latest_job.original_filename
            or project_title
        )
        rel_note = str(preview.get("relationship_note") or "").strip() or None

    md = render_lab_profile_markdown(
        project_title=project_title,
        as_of_time=as_of_date,
        people=people,
        projects=projects,
        achievements=achievements,
        facilities=facilities,
        relationship_note=rel_note,
    )
    assert_no_pii(md)

    if fmt == "md":
        return Response(
            content=md.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="lab-assets.md"'},
        )

    doc = docx.Document()
    doc.add_heading(f"课题组资料库：{project_title}", level=0)
    doc.add_paragraph(
        f"基准时间：{as_of_date.isoformat()}。团队成员 {len(people)} 人，"
        f"承担项目 {len(projects)} 项，前期成果 {len(achievements)} 项，"
        f"设备与支撑条件 {len(facilities)} 项。"
    )

    doc.add_heading("一、团队成员", level=1)
    for idx, person in enumerate(people, start=1):
        doc.add_paragraph(f"[{idx}] {format_person_application(person, include_bio=True)}")

    doc.add_heading("二、承担项目", level=1)
    for idx, proj in enumerate(projects, start=1):
        doc.add_paragraph(
            f"[{idx}] {format_project_application(proj, include_relationship=True)}"
        )

    doc.add_heading("三、前期成果", level=1)
    for idx, ach in enumerate(achievements, start=1):
        doc.add_paragraph(
            f"[{idx}] {format_achievement_application(ach, include_support=True)}"
        )

    doc.add_heading("四、设备与支撑条件", level=1)
    for idx, fac in enumerate(facilities, start=1):
        doc.add_paragraph(f"[{idx}] {format_facility_application(fac)}")

    if rel_note:
        doc.add_heading("五、与已承担项目的关系", level=1)
        doc.add_paragraph(rel_note)

    all_doc_text = "\n".join(p.text for p in doc.paragraphs)
    assert_no_pii(all_doc_text)

    buf = io.BytesIO()
    doc.save(buf)
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": 'attachment; filename="lab-assets.docx"'},
    )
