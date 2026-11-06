"""API routes for Proposal Cold-Start Import (/api/v1/admin/imports) and Lab Assets Hub (/api/v1/lab)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.formatters import (
    ACHIEVEMENT_KIND_LABELS,
    PROJECT_STATUS_LABELS,
    format_achievement_application,
    format_facility_application,
    format_person_application,
    format_project_application,
)
from app.importer.parser import parse_proposal_docx
from app.importer.provision import (
    ProvisionError,
    ProvisionRequest,
    provision_import_job,
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
        "status_label": PROJECT_STATUS_LABELS.get(project.status, "进行中"),
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

    try:
        parsed_bundle: dict[str, Any] = parse_proposal_docx(
            filename,
            raw_bytes,
            existing_users=existing_users,
            as_of_time_override=override_date,
            max_file_mb=settings.import_max_file_mb,
            max_uncompressed_mb=settings.import_max_uncompressed_mb,
        )
    except DocxValidationError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc

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
        content_sha256=str(parsed_bundle.get("content_sha256") or ""),
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
    return serialized


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
