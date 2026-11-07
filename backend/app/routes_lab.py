"""API routes for Proposal Cold-Start Import (/api/v1/admin/imports) and Lab Assets Hub (/api/v1/lab)."""

from __future__ import annotations

import io
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import docx
from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer
from starlette.exceptions import HTTPException

from app.audit import write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.basket import (
    MIME_TO_EXT,
    BasketError,
    build_rfc5987_content_disposition,
    build_selected_docx,
    fetch_and_format_batch_items,
    iter_bytes_chunks,
    prepare_signatures_zip,
    sanitize_filename_part,
)
from app.importer.fingerprint import achievement_fp, facility_fp, person_fp, project_fp
from app.importer.formatters import (
    ACHIEVEMENT_KIND_LABELS,
    PROJECT_STATUS_LABELS,
    format_achievement_application,
    format_facility_application,
    format_person_application,
    format_project_application,
    render_lab_profile_markdown,
)
from app.importer.matching import (
    build_ilike_patterns,
    classify_achievement_for_person,
    classify_project_for_person,
)
from app.importer.matching import (
    normalize_person_name as normalize_dossier_name,
)
from app.importer.parser import parse_proposal_docx, reparse_preview_payload
from app.importer.people import assign_people_identities, clean_email, normalize_person_name
from app.importer.pii import PIILeakError, assert_no_pii, scrub_free_text
from app.importer.provenance import (
    ACHIEVEMENT_MANAGED_FIELDS,
    FACILITY_MANAGED_FIELDS,
    PERSON_MANAGED_FIELDS,
    PROJECT_MANAGED_FIELDS,
    MergePreviewRequest,
    MergeRequest,
    backfill_field_provenance,
    build_merge_preview,
    build_provenance_map,
    execute_merge,
    fallback_entity_provenance_summary,
    get_entity_json_value,
    record_entity_import_provenance,
    record_manual_field_edit,
    restore_entity_imported_fields,
    revert_import_merge,
)
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
from app.importer.signature import validate_signature_image
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


class BatchFormatItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: str
    id: uuid.UUID | int | str


class BatchFormatOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: str = "nsfc_standard"
    numbering: str = "bracket"
    group_by_entity: bool = True
    include_person_bio: bool = False
    include_support_note: bool = True
    include_signatures_annex: bool = False


class BatchFormatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # v2.2 basket items + options
    items: list[BatchFormatItem] | None = None
    options: BatchFormatOptions | None = None

    # v2.0 tab-based backward compatibility
    tab: Literal["people", "projects", "achievements", "facilities"] | None = None
    ids: list[uuid.UUID] | None = None
    numbered: bool = True
    only_ongoing: bool = False
    recent_years: int | None = None
    with_support: bool = True
    include_bio: bool = False
    include_relationship: bool = False


class SignaturesZipBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    person_ids: list[uuid.UUID | int | str] = Field(default_factory=list)
    filename_template: str = "seq_name"


class ExportSelectedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    items: list[BatchFormatItem] = Field(default_factory=list)
    options: BatchFormatOptions = Field(default_factory=BatchFormatOptions)


def _lab_v22_error(
    status: int, code: str, message: str, **extra: Any
) -> JSONResponse:
    err_obj: dict[str, Any] = {"code": code, "message": message}
    for k, v in extra.items():
        if v is not None:
            err_obj[k] = v
    return JSONResponse(
        status_code=status,
        content={"error": err_obj, "detail": err_obj},
    )


class RestoreImportedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str | None = None
    fields: list[str] | None = None


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
        return {
            k: (v if str(k).startswith("_") else _scrub_obj(v))
            for k, v in obj.items()
        }
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
        if status_source == "inferred":
            if end_d is not None and as_of_date is not None:
                proj["status"] = "completed" if end_d < as_of_date else "ongoing"
            elif end_d is None:
                proj["status"] = "unknown"
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
    raw_stats = job.stats or {}
    clean_stats = {
        k: v for k, v in raw_stats.items() if not str(k).startswith("_")
    }
    payload: dict[str, object] = {
        "id": str(job.id),
        "job_id": str(job.id),
        "uploaded_by": str(job.uploaded_by),
        "original_filename": job.original_filename,
        "content_sha256": job.content_sha256,
        "parser_version": job.parser_version,
        "as_of_time": job.as_of_time.isoformat() if job.as_of_time else None,
        "status": job.status,
        "stats": clean_stats,
        "error_code": job.error_code,
        "rag_document_id": str(job.rag_document_id) if job.rag_document_id else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "provisioned_at": job.provisioned_at.isoformat() if job.provisioned_at else None,
        "provisioned_by": str(job.provisioned_by) if job.provisioned_by else None,
    }
    if include_preview:
        raw_prev = job.preview or {}
        payload["preview"] = {
            k: v for k, v in raw_prev.items() if not str(k).startswith("_")
        }
    return payload


def _serialize_person(
    person: LabPerson,
    provenance: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    has_sig = bool(person.signature_sha256 and person.signature_mime)
    copy_txt = format_person_application(person, include_bio=False)
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
        "org": person.affiliation,
        "project_role": person.project_role,
        "role": "pi" if person.project_role == "applicant" else "member",
        "bio": person.bio,
        "bio_text": person.bio,
        "copy_text": copy_txt,
        "has_signature": has_sig,
        "signature_mime": person.signature_mime,
        "signature_sha256": person.signature_sha256,
        "signature_updated_at": (
            person.signature_updated_at.isoformat()
            if person.signature_updated_at
            else None
        ),
        "signature_source": person.signature_source,
        "signature_url": (
            f"/api/v1/lab/people/{person.id}/signature" if has_sig else None
        ),
        "entity_fingerprint": person.entity_fingerprint,
        "as_of_time": person.as_of_time.isoformat() if person.as_of_time else None,
        "source_import_id": (
            str(person.source_import_id) if person.source_import_id else None
        ),
        "application_text": copy_txt,
        "application_text_with_bio": format_person_application(person, include_bio=True),
        "provenance": (
            provenance
            if provenance is not None
            else fallback_entity_provenance_summary("person", person)
        ),
    }


def _serialize_project(
    project: LabProject,
    provenance: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
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
        "entity_fingerprint": project.entity_fingerprint,
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
        "provenance": (
            provenance
            if provenance is not None
            else fallback_entity_provenance_summary("project", project)
        ),
    }


def _serialize_achievement(
    achievement: LabAchievement,
    provenance: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
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
        "entity_fingerprint": achievement.entity_fingerprint,
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
        "provenance": (
            provenance
            if provenance is not None
            else fallback_entity_provenance_summary("achievement", achievement)
        ),
    }


def _serialize_facility(
    facility: LabFacility,
    provenance: dict[str, dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "id": str(facility.id),
        "category": facility.category,
        "name": facility.name,
        "spec": facility.spec,
        "value_wan": str(facility.value_wan) if facility.value_wan is not None else None,
        "usage_scenario": facility.usage_scenario,
        "raw_text": facility.raw_text,
        "entity_fingerprint": facility.entity_fingerprint,
        "as_of_time": facility.as_of_time.isoformat() if facility.as_of_time else None,
        "source_import_id": (
            str(facility.source_import_id) if facility.source_import_id else None
        ),
        "application_text": format_facility_application(facility),
        "provenance": (
            provenance
            if provenance is not None
            else fallback_entity_provenance_summary("facility", facility)
        ),
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
    has_referenced_assets = (
        await session.scalar(
            select(LabPerson.id).where(LabPerson.source_import_id == job_id).limit(1)
        )
        is not None
        or await session.scalar(
            select(LabProject.id).where(LabProject.source_import_id == job_id).limit(1)
        )
        is not None
        or await session.scalar(
            select(LabAchievement.id)
            .where(LabAchievement.source_import_id == job_id)
            .limit(1)
        )
        is not None
        or await session.scalar(
            select(LabFacility.id).where(LabFacility.source_import_id == job_id).limit(1)
        )
        is not None
    )
    if has_referenced_assets:
        raise APIError(
            409,
            "import_already_provisioned",
            "该导入任务存在关联的实验室资产，不可删除",
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
    if effective_mode == "llm" and _settings(request).llm_provider == "fake":
        raise APIError(
            422,
            "llm_reparse_unavailable",
            "当前未配置在线大模型服务，无法使用 llm 模式重新解析，请使用 fsm 模式",
        )

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

    preview, recomputed_stats = reparse_preview_payload(
        preview,
        as_of_date=job.as_of_time,
        mode=effective_mode,
        existing_users=existing_users,
        existing_people=existing_people,
    )
    parser_ver = str(preview.get("parser_version") or "v2.1-fsm")
    job.parser_version = parser_ver
    job.preview = preview
    cur_stats = dict(job.stats or {})
    cur_stats.update(recomputed_stats)
    job.stats = cur_stats
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
@router.post("/admin/imports/{job_id}/confirm")
async def provision_import(
    job_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: ProvisionRequest | None = Body(default=None),
) -> dict[str, object]:
    effective_body = body if body is not None else ProvisionRequest()
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
            body=effective_body,
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
            "include_password": bool(effective_body.include_password),
        },
    )
    return result


@router.post("/admin/imports/{job_id}/merge-preview")
async def merge_preview_import_job(
    job_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: MergePreviewRequest | None = Body(default=None),
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="import_merge_preview",
        resource_type="import_job",
        resource_id=str(job_id),
    )
    try:
        preview = await build_merge_preview(
            session,
            job_id=job_id,
            as_of_time_override=body.as_of_time if body is not None else None,
        )
    except ProvisionError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc
    await session.commit()
    return preview


@router.post("/admin/imports/{job_id}/merge")
async def merge_import_job(
    job_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    body: MergeRequest | None = Body(default=None),
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="import_proposal_merge",
        resource_type="import_job",
        resource_id=str(job_id),
    )
    merge_body = body if body is not None else MergeRequest()
    try:
        result = await execute_merge(
            session,
            job_id=job_id,
            actor_id=actor.id,
            body=merge_body,
            embedder=_embedder(request),
        )
    except ProvisionError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc

    await session.commit()
    raw_stats = result.get("stats")
    stats: dict[str, Any] = raw_stats if isinstance(raw_stats, dict) else {}
    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="import_proposal_merged",
        resource_type="import_job",
        resource_id=str(job_id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "added_entities": int(stats.get("added_entities") or 0),
            "updated_fields": int(stats.get("updated_fields") or 0),
            "kept_fields": int(stats.get("kept_fields") or 0),
            "forced_fields": int(stats.get("forced_fields") or 0),
        },
    )
    return result


@router.post("/admin/imports/{job_id}/revert")
async def revert_import_job(
    job_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="import_merge_revert",
        resource_type="import_job",
        resource_id=str(job_id),
    )
    try:
        result = await revert_import_merge(
            session,
            job_id=job_id,
            actor_id=actor.id,
            embedder=_embedder(request),
        )
    except ProvisionError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc

    await session.commit()
    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="import_merge_reverted",
        resource_type="import_job",
        resource_id=str(job_id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "reverted_fields": result.get("reverted_fields"),
            "deleted_entities": result.get("deleted_entities"),
            "job_status": str(result.get("job_status") or ""),
        },
    )
    return result


@router.post("/admin/imports/backfill-provenance")
async def backfill_imports_provenance(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="import_backfill_provenance",
        resource_type="import_job",
    )
    result = await backfill_field_provenance(session)
    await session.commit()
    raw_disc = result.get("discrepancies")
    disc_count = len(raw_disc) if isinstance(raw_disc, list) else 0
    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="import_provenance_backfilled",
        resource_type="import_job",
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "backfilled_entries": result.get("backfilled_entries"),
            "discrepancies": disc_count,
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
        .where(ImportJob.status.in_(("provisioned", "merged")))
        .order_by(ImportJob.provisioned_at.desc(), ImportJob.created_at.desc())
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
    prov_map = await build_provenance_map(session, "person", rows)
    return {
        "people": [_serialize_person(p, provenance=prov_map.get(p.id)) for p in rows]
    }


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
    prov_map = await build_provenance_map(session, "project", rows)
    return {
        "projects": [_serialize_project(p, provenance=prov_map.get(p.id)) for p in rows]
    }


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
    prov_map = await build_provenance_map(session, "achievement", rows)
    return {
        "achievements": [
            _serialize_achievement(a, provenance=prov_map.get(a.id)) for a in rows
        ]
    }


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
    prov_map = await build_provenance_map(session, "facility", rows)
    return {
        "facilities": [
            _serialize_facility(f, provenance=prov_map.get(f.id)) for f in rows
        ]
    }


async def _resolve_source_import_id(
    session: AsyncSession,
    actor_id: uuid.UUID,
    as_of_date: date,
    explicit_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, date]:
    if explicit_id is not None:
        job = await session.get(ImportJob, explicit_id)
        if job is None:
            raise APIError(404, "not_found", "关联的导入任务不存在")
        if job.status not in ("provisioned", "merged"):
            raise APIError(
                409,
                "import_not_provisioned",
                "仅可关联已开号或已合并的导入任务",
            )
        return job.id, job.as_of_time or as_of_date
    latest_job = await session.scalar(
        select(ImportJob)
        .where(ImportJob.status.in_(("provisioned", "merged")))
        .order_by(ImportJob.provisioned_at.desc(), ImportJob.created_at.desc())
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
    new_owner_name: str | None = None,
    require_owner: bool = False,
    admin_only: bool = False,
) -> User:
    user = await _user(session, actor)
    if actor.role == "admin":
        return user
    if admin_only:
        raise APIError(403, "forbidden", "仅管理员可修改或删除公共设备记录")
    if actor.role != "member":
        raise APIError(403, "forbidden", "权限不足")
    if require_owner or owner_name is not None or new_owner_name is not None:
        person = await session.scalar(
            select(LabPerson).where(LabPerson.user_id == user.id)
        )
        allowed_names = {
            n
            for n in (
                normalize_person_name(user.display_name),
                normalize_person_name(person.name) if person else "",
            )
            if n
        }
        if require_owner or owner_name is not None:
            norm_owner = normalize_person_name(owner_name or "")
            if not norm_owner:
                raise APIError(
                    403,
                    "forbidden",
                    "未登记负责人的记录仅管理员可修改或删除",
                )
            if norm_owner not in allowed_names:
                raise APIError(403, "forbidden", "仅管理员或条目负责人可修改此记录")
            all_people = list(await session.scalars(select(LabPerson)))
            same_name_count = sum(
                1
                for p in all_people
                if normalize_person_name(p.name) == norm_owner
            )
            if same_name_count > 1:
                raise APIError(
                    403,
                    "forbidden",
                    "存在同名成员歧义，仅管理员可修改此记录",
                )
        if new_owner_name is not None:
            norm_new = normalize_person_name(new_owner_name)
            if not norm_new or norm_new not in allowed_names:
                raise APIError(403, "forbidden", "非管理员不可将条目负责人改为他人")
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

    old_name = person.name
    old_affiliation = person.affiliation
    old_email = person.email
    old_fp = person.entity_fingerprint
    old_t2_t3_fp = person_fp(
        tier="T3" if old_email else "T2",
        login_name=None,
        name=old_name,
        affiliation=old_affiliation,
        email=old_email,
    )
    is_t1_person = old_fp != old_t2_t3_fp
    t1_login: str | None = None
    if is_t1_person and person.user_id is not None:
        linked_user = await session.get(User, person.user_id)
        if linked_user is not None and linked_user.login_name:
            candidate_t1_fp = person_fp(
                tier="T1",
                login_name=linked_user.login_name,
                name=old_name,
                affiliation=old_affiliation,
                email=old_email,
            )
            if candidate_t1_fp == old_fp:
                t1_login = linked_user.login_name

    old_vals = {
        f: get_entity_json_value("person", person, f)
        for f in PERSON_MANAGED_FIELDS
    }
    touched_fields: list[str] = []

    fields_set = body.model_fields_set
    if "name" in fields_set:
        cleaned_name = normalize_person_name(_scrub_str(body.name) or "")
        if not cleaned_name:
            raise APIError(422, "invalid_name", "姓名不能为空")
        if actor.role != "admin" and cleaned_name != normalize_person_name(person.name):
            raise APIError(403, "forbidden", "仅管理员可修改成员姓名或项目角色")
        person.name = cleaned_name
    if "email" in fields_set:
        person.email = clean_email(body.email)
        touched_fields.append("email")
    if "title" in fields_set:
        person.title = _scrub_str(body.title)
        touched_fields.append("title")
    if "admin_duty" in fields_set:
        person.admin_duty = _scrub_str(body.admin_duty)
        touched_fields.append("admin_duty")
    if "tutor_role" in fields_set:
        person.tutor_role = _scrub_str(body.tutor_role)
        touched_fields.append("tutor_role")
    if "research_specialty" in fields_set:
        person.research_specialty = _scrub_str(body.research_specialty)
        touched_fields.append("research_specialty")
    if "degree" in fields_set:
        person.degree = _scrub_str(body.degree)
        touched_fields.append("degree")
    if "affiliation" in fields_set:
        person.affiliation = _scrub_str(body.affiliation)
        touched_fields.append("affiliation")
    if "project_role" in fields_set and body.project_role is not None:
        pr = body.project_role.strip().lower()
        if pr not in ("applicant", "member"):
            raise APIError(422, "invalid_project_role", "project_role 仅支持 applicant 或 member")
        if actor.role != "admin" and pr != person.project_role:
            raise APIError(403, "forbidden", "仅管理员可修改成员姓名或项目角色")
        person.project_role = pr
        touched_fields.append("project_role")
    if "bio" in fields_set:
        person.bio = _scrub_str(body.bio)
        touched_fields.append("bio")

    if any(f in fields_set for f in ("name", "affiliation", "email")):
        if is_t1_person:
            new_person_fp = (
                person_fp(
                    tier="T1",
                    login_name=t1_login,
                    name=person.name,
                    affiliation=person.affiliation,
                    email=person.email,
                )
                if t1_login
                else old_fp
            )
        else:
            new_person_fp = person_fp(
                tier="T3" if person.email else "T2",
                login_name=None,
                name=person.name,
                affiliation=person.affiliation,
                email=person.email,
            )
        dup_person_id = await session.scalar(
            select(LabPerson.id).where(
                LabPerson.entity_fingerprint == new_person_fp,
                LabPerson.id != person.id,
            )
        )
        if dup_person_id is not None:
            raise APIError(409, "duplicate_entity", "修改后的记录与已有资产指纹冲突")
        person.entity_fingerprint = new_person_fp

    for fld in touched_fields:
        await record_manual_field_edit(
            session,
            entity_type="person",
            entity=person,
            field=fld,
            old_val=old_vals.get(fld),
            new_val=get_entity_json_value("person", person, fld),
            actor_id=user.id,
        )
    await session.flush()

    prov_map = await build_provenance_map(session, "person", [person])
    serialized_person = _serialize_person(person, provenance=prov_map.get(person.id))
    assert_no_pii(serialized_person)

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
    return {"person": serialized_person, "rag_doc_key": doc_key}


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

    fields_set = body.model_fields_set
    funder = _scrub_str(body.funder)
    grant_no = _scrub_str(body.grant_no)
    pi_name = _scrub_str(body.pi_name)
    lab_role = (body.lab_role or "host").strip().lower()
    if lab_role not in ("host", "participant", "unknown"):
        lab_role = "host"
    start_d = _parse_iso_date(body.start_date)
    end_d = _parse_iso_date(body.end_date)
    amt_wan = _parse_decimal(body.amount_wan)
    explicit_status = (
        body.status.strip().lower()
        if "status" in fields_set and body.status is not None and body.status.strip()
        else None
    )
    status = (
        explicit_status
        if explicit_status in ("ongoing", "completed", "unknown")
        else "ongoing"
    )
    if (
        "status_source" in fields_set
        and body.status_source is not None
        and body.status_source.strip()
    ):
        ss_raw = body.status_source.strip().lower()
        status_source = ss_raw if ss_raw in ("explicit", "inferred") else "explicit"
    elif explicit_status is None and end_d is not None:
        status_source = "inferred"
    else:
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
    if status_source == "inferred" and explicit_status is None:
        if end_d is not None and as_of_date is not None:
            status = "completed" if end_d < as_of_date else "ongoing"
        else:
            status = "unknown"
    fp_val = project_fp(grant_no=grant_no, title=title, funder=funder)
    existing_fp = await session.scalar(
        select(LabProject.id).where(LabProject.entity_fingerprint == fp_val)
    )
    if existing_fp is not None:
        raise APIError(409, "duplicate_entity", "该资产记录已存在（指纹冲突）")

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
    await record_entity_import_provenance(
        session,
        entity_type="project",
        entity=project,
        job_id=body.source_import_id,
        as_of_time=as_of_date,
        recorded_by=user.id,
        source_kind="import" if body.source_import_id is not None else "manual",
    )
    await session.flush()
    prov_map = await build_provenance_map(session, "project", [project])
    serialized_proj = _serialize_project(project, provenance=prov_map.get(project.id))
    assert_no_pii(serialized_proj)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"project": serialized_proj, "rag_doc_key": doc_key}


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
    fields_set = body.model_fields_set
    user = await _require_lab_editor(
        session,
        actor,
        owner_name=project.pi_name,
        new_owner_name=(_scrub_str(body.pi_name) or "")
        if "pi_name" in fields_set
        else None,
        require_owner=True,
    )

    old_vals = {
        f: get_entity_json_value("project", project, f)
        for f in PROJECT_MANAGED_FIELDS
    }
    touched_fields: list[str] = []

    if "title" in fields_set:
        t_clean = _scrub_str(body.title)
        if not t_clean:
            raise APIError(422, "invalid_title", "项目名称不能为空")
        project.title = t_clean
        touched_fields.append("title")
    if "funder" in fields_set:
        project.funder = _scrub_str(body.funder)
        touched_fields.append("funder")
    if "grant_no" in fields_set:
        project.grant_no = _scrub_str(body.grant_no)
        touched_fields.append("grant_no")
    if "pi_name" in fields_set:
        project.pi_name = _scrub_str(body.pi_name)
        touched_fields.append("pi_name")
    if "lab_role" in fields_set and body.lab_role is not None:
        lr = body.lab_role.strip().lower()
        if lr in ("host", "participant", "unknown"):
            project.lab_role = lr
            touched_fields.append("lab_role")
    if "start_date" in fields_set:
        project.start_date = _parse_iso_date(body.start_date)
        touched_fields.append("start_date")
    if "end_date" in fields_set:
        project.end_date = _parse_iso_date(body.end_date)
        touched_fields.append("end_date")
    if "amount_wan" in fields_set:
        project.amount_wan = _parse_decimal(body.amount_wan)
        touched_fields.append("amount_wan")
    if "as_of_time" in fields_set and body.as_of_time is not None:
        parsed_as_of = _parse_iso_date(body.as_of_time)
        if parsed_as_of is not None:
            project.as_of_time = parsed_as_of
    explicit_status = (
        body.status.strip().lower()
        if "status" in fields_set and body.status is not None and body.status.strip()
        else None
    )
    if explicit_status is not None and explicit_status in (
        "ongoing",
        "completed",
        "unknown",
    ):
        project.status = explicit_status
        touched_fields.append("status")
        if "status_source" not in fields_set or not (body.status_source or "").strip():
            project.status_source = "explicit"
            touched_fields.append("status_source")
    if "status_source" in fields_set and body.status_source is not None:
        ss = body.status_source.strip().lower()
        if ss in ("explicit", "inferred"):
            project.status_source = ss
            if "status_source" not in touched_fields:
                touched_fields.append("status_source")
    if project.status_source == "inferred" and explicit_status is None:
        if project.end_date is not None and project.as_of_time is not None:
            project.status = (
                "completed" if project.end_date < project.as_of_time else "ongoing"
            )
            if "status" not in touched_fields:
                touched_fields.append("status")
        elif project.end_date is None:
            project.status = "unknown"
            if "status" not in touched_fields:
                touched_fields.append("status")
    if "relationship_note" in fields_set:
        project.relationship_note = _scrub_str(body.relationship_note)
        touched_fields.append("relationship_note")
    if "raw_text" in fields_set:
        project.raw_text = _scrub_str(body.raw_text) or project.title
        touched_fields.append("raw_text")

    new_proj_fp = project_fp(
        grant_no=project.grant_no,
        title=project.title,
        funder=project.funder,
    )
    dup_proj_id = await session.scalar(
        select(LabProject.id).where(
            LabProject.entity_fingerprint == new_proj_fp,
            LabProject.id != project.id,
        )
    )
    if dup_proj_id is not None:
        raise APIError(409, "duplicate_entity", "修改后的记录与已有资产指纹冲突")
    project.entity_fingerprint = new_proj_fp

    status_changed = old_vals.get("status") != get_entity_json_value(
        "project", project, "status"
    )
    for fld in touched_fields:
        await record_manual_field_edit(
            session,
            entity_type="project",
            entity=project,
            field=fld,
            old_val=old_vals.get(fld),
            new_val=get_entity_json_value("project", project, fld),
            actor_id=user.id,
            force_record=(fld == "status_source" and status_changed),
        )
    await session.flush()
    prov_map = await build_provenance_map(session, "project", [project])
    serialized_proj = _serialize_project(project, provenance=prov_map.get(project.id))
    assert_no_pii(serialized_proj)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"project": serialized_proj, "rag_doc_key": doc_key}


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
    user = await _require_lab_editor(
        session, actor, owner_name=project.pi_name, require_owner=True
    )

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
        raise APIError(409, "duplicate_entity", "该资产记录已存在（指纹冲突）")

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
    await record_entity_import_provenance(
        session,
        entity_type="achievement",
        entity=achievement,
        job_id=body.source_import_id,
        as_of_time=as_of_date,
        recorded_by=user.id,
        source_kind="import" if body.source_import_id is not None else "manual",
    )
    await session.flush()
    prov_map = await build_provenance_map(session, "achievement", [achievement])
    serialized_ach = _serialize_achievement(
        achievement, provenance=prov_map.get(achievement.id)
    )
    assert_no_pii(serialized_ach)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"achievement": serialized_ach, "rag_doc_key": doc_key}


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
    fields_set = body.model_fields_set
    user = await _require_lab_editor(
        session,
        actor,
        owner_name=achievement.first_author,
        new_owner_name=(_scrub_str(body.first_author) or "")
        if "first_author" in fields_set
        else None,
        require_owner=True,
    )

    old_vals = {
        f: get_entity_json_value("achievement", achievement, f)
        for f in ACHIEVEMENT_MANAGED_FIELDS
    }
    touched_fields: list[str] = []

    if "kind" in fields_set and body.kind is not None:
        k_clean = body.kind.strip().lower()
        if k_clean in ACHIEVEMENT_KIND_LABELS:
            achievement.kind = k_clean
            touched_fields.append("kind")
    if "title" in fields_set:
        t_clean = _scrub_str(body.title)
        if not t_clean:
            raise APIError(422, "invalid_title", "成果名称不能为空")
        achievement.title = t_clean
        touched_fields.append("title")
    if "first_author" in fields_set:
        achievement.first_author = _scrub_str(body.first_author)
        touched_fields.append("first_author")
    if "authors_text" in fields_set:
        achievement.authors_text = _scrub_str(body.authors_text)
        touched_fields.append("authors_text")
    if "venue" in fields_set:
        achievement.venue = _scrub_str(body.venue)
        touched_fields.append("venue")
    if "year" in fields_set:
        achievement.year = body.year
        touched_fields.append("year")
    if "support_note" in fields_set:
        achievement.support_note = _scrub_str(body.support_note)
        touched_fields.append("support_note")
    if "raw_text" in fields_set:
        achievement.raw_text = _scrub_str(body.raw_text) or achievement.title
        touched_fields.append("raw_text")

    new_ach_fp = achievement_fp(
        kind=achievement.kind,
        title=achievement.title,
        year=achievement.year,
        raw_text=achievement.raw_text,
        parse_quality=achievement.parse_quality,
    )
    dup_ach_id = await session.scalar(
        select(LabAchievement.id).where(
            LabAchievement.entity_fingerprint == new_ach_fp,
            LabAchievement.id != achievement.id,
        )
    )
    if dup_ach_id is not None:
        raise APIError(409, "duplicate_entity", "修改后的记录与已有资产指纹冲突")
    achievement.entity_fingerprint = new_ach_fp

    for fld in touched_fields:
        await record_manual_field_edit(
            session,
            entity_type="achievement",
            entity=achievement,
            field=fld,
            old_val=old_vals.get(fld),
            new_val=get_entity_json_value("achievement", achievement, fld),
            actor_id=user.id,
        )
    await session.flush()
    prov_map = await build_provenance_map(session, "achievement", [achievement])
    serialized_ach = _serialize_achievement(
        achievement, provenance=prov_map.get(achievement.id)
    )
    assert_no_pii(serialized_ach)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"achievement": serialized_ach, "rag_doc_key": doc_key}


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
        session,
        actor,
        owner_name=achievement.first_author,
        require_owner=True,
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
        raise APIError(409, "duplicate_entity", "该资产记录已存在（指纹冲突）")

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
    await record_entity_import_provenance(
        session,
        entity_type="facility",
        entity=facility,
        job_id=body.source_import_id,
        as_of_time=as_of_date,
        recorded_by=user.id,
        source_kind="import" if body.source_import_id is not None else "manual",
    )
    await session.flush()
    prov_map = await build_provenance_map(session, "facility", [facility])
    serialized_fac = _serialize_facility(facility, provenance=prov_map.get(facility.id))
    assert_no_pii(serialized_fac)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"facility": serialized_fac, "rag_doc_key": doc_key}


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
    user = await _require_lab_editor(session, actor, admin_only=True)
    fields_set = body.model_fields_set

    old_vals = {
        f: get_entity_json_value("facility", facility, f)
        for f in FACILITY_MANAGED_FIELDS
    }
    touched_fields: list[str] = []

    if "category" in fields_set and body.category is not None:
        facility.category = _scrub_str(body.category) or facility.category
        touched_fields.append("category")
    if "name" in fields_set:
        n_clean = _scrub_str(body.name)
        if not n_clean:
            raise APIError(422, "invalid_name", "设备名称不能为空")
        facility.name = n_clean
        touched_fields.append("name")
    if "spec" in fields_set:
        facility.spec = _scrub_str(body.spec)
        touched_fields.append("spec")
    if "value_wan" in fields_set:
        facility.value_wan = _parse_decimal(body.value_wan)
        touched_fields.append("value_wan")
    if "usage_scenario" in fields_set:
        facility.usage_scenario = _scrub_str(body.usage_scenario)
        touched_fields.append("usage_scenario")
    if "raw_text" in fields_set:
        facility.raw_text = _scrub_str(body.raw_text) or facility.name
        touched_fields.append("raw_text")

    new_fac_fp = facility_fp(
        category=facility.category,
        name=facility.name,
        spec=facility.spec,
    )
    dup_fac_id = await session.scalar(
        select(LabFacility.id).where(
            LabFacility.entity_fingerprint == new_fac_fp,
            LabFacility.id != facility.id,
        )
    )
    if dup_fac_id is not None:
        raise APIError(409, "duplicate_entity", "修改后的记录与已有资产指纹冲突")
    facility.entity_fingerprint = new_fac_fp

    for fld in touched_fields:
        await record_manual_field_edit(
            session,
            entity_type="facility",
            entity=facility,
            field=fld,
            old_val=old_vals.get(fld),
            new_val=get_entity_json_value("facility", facility, fld),
            actor_id=user.id,
        )
    await session.flush()
    prov_map = await build_provenance_map(session, "facility", [facility])
    serialized_fac = _serialize_facility(facility, provenance=prov_map.get(facility.id))
    assert_no_pii(serialized_fac)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"facility": serialized_fac, "rag_doc_key": doc_key}


@router.post("/lab/{entity_type}/{entity_id}/restore-imported")
async def restore_lab_entity_imported(
    entity_type: str,
    entity_id: uuid.UUID,
    body: RestoreImportedBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    raw_type = entity_type.strip().lower()
    type_map: dict[str, Literal["person", "project", "achievement", "facility"]] = {
        "people": "person",
        "person": "person",
        "projects": "project",
        "project": "project",
        "achievements": "achievement",
        "achievement": "achievement",
        "facilities": "facility",
        "facility": "facility",
    }
    norm_type = type_map.get(raw_type)
    if norm_type is None:
        raise APIError(400, "invalid_entity_type", f"不支持的实体类型: {entity_type}")

    fields_to_restore: list[str] = []
    if body.field and body.field.strip():
        fields_to_restore.append(body.field.strip())
    if body.fields:
        for f in body.fields:
            if f and f.strip() and f.strip() not in fields_to_restore:
                fields_to_restore.append(f.strip())
    if not fields_to_restore:
        raise APIError(422, "field_required", "请提供需要恢复为导入值的字段名 field")

    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))

    entity: Any
    user: User
    if norm_type == "person":
        user = await _user(session, actor)
        entity = await session.scalar(
            select(LabPerson).where(LabPerson.id == entity_id).with_for_update()
        )
        if entity is None:
            raise APIError(404, "not_found", "成员档案不存在")
        if actor.role != "admin" and entity.user_id != user.id:
            raise APIError(403, "forbidden", "仅管理员或本人可恢复成员字段")
        if "project_role" in fields_to_restore and actor.role != "admin":
            await _require_admin(
                request,
                actor,
                operation="restore_lab_person_project_role",
                resource_type="lab_person",
                resource_id=str(entity_id),
            )
    elif norm_type == "project":
        entity = await session.scalar(
            select(LabProject).where(LabProject.id == entity_id).with_for_update()
        )
        if entity is None:
            raise APIError(404, "not_found", "项目记录不存在")
        user = await _require_lab_editor(
            session, actor, owner_name=entity.pi_name, require_owner=True
        )
    elif norm_type == "achievement":
        entity = await session.scalar(
            select(LabAchievement)
            .where(LabAchievement.id == entity_id)
            .with_for_update()
        )
        if entity is None:
            raise APIError(404, "not_found", "成果记录不存在")
        user = await _require_lab_editor(
            session, actor, owner_name=entity.first_author, require_owner=True
        )
    else:
        entity = await session.scalar(
            select(LabFacility).where(LabFacility.id == entity_id).with_for_update()
        )
        if entity is None:
            raise APIError(404, "not_found", "设备记录不存在")
        user = await _require_lab_editor(session, actor, admin_only=True)

    try:
        restored = await restore_entity_imported_fields(
            session,
            entity_type=norm_type,
            entity=entity,
            fields=fields_to_restore,
        )
    except ProvisionError as exc:
        raise APIError(exc.status, exc.code, exc.message) from exc

    if norm_type == "project" and "pi_name" in fields_to_restore:
        await _require_lab_editor(
            session,
            actor,
            owner_name=entity.pi_name,
            new_owner_name=entity.pi_name or "",
            require_owner=True,
        )
    elif norm_type == "achievement" and "first_author" in fields_to_restore:
        await _require_lab_editor(
            session,
            actor,
            owner_name=entity.first_author,
            new_owner_name=entity.first_author or "",
            require_owner=True,
        )

    await session.flush()
    prov_map = await build_provenance_map(session, norm_type, [entity])
    if norm_type == "person":
        serialized = _serialize_person(entity, provenance=prov_map.get(entity.id))
    elif norm_type == "project":
        serialized = _serialize_project(entity, provenance=prov_map.get(entity.id))
    elif norm_type == "achievement":
        serialized = _serialize_achievement(entity, provenance=prov_map.get(entity.id))
    else:
        serialized = _serialize_facility(entity, provenance=prov_map.get(entity.id))
    assert_no_pii(serialized)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_field_restored_imported",
        resource_type=f"lab_{norm_type}",
        resource_id=str(entity.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"fields": restored, "rag_doc_key": doc_key},
    )
    return {
        **serialized,
        norm_type: serialized,
        "item": serialized,
        "restored_fields": restored,
        "rag_doc_key": doc_key,
    }


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
    user = await _require_lab_editor(session, actor, admin_only=True)

    await session.delete(facility)
    await session.flush()
    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await session.commit()
    return {"status": "deleted", "id": str(facility_id), "rag_doc_key": doc_key}


@router.post("/lab/format/batch", response_model=None)
async def batch_format_lab_assets(
    body: BatchFormatBody,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object] | JSONResponse:
    await _user(session, actor)

    # v2.2 Basket items + options mode
    if body.items is not None:
        opts = body.options or BatchFormatOptions()
        raw_items = [{"entity": it.entity, "id": it.id} for it in body.items]
        try:
            res = await fetch_and_format_batch_items(
                session,
                raw_items=raw_items,
                style=opts.style,
                numbering=opts.numbering,
                group_by_entity=opts.group_by_entity,
                include_person_bio=opts.include_person_bio,
                include_support_note=opts.include_support_note,
            )
        except BasketError as exc:
            return _lab_v22_error(
                exc.status,
                exc.code,
                exc.message,
                missing=exc.missing,
            )
        combined = str(res["combined_text"] or "")
        assert_no_pii(combined)
        items_out: list[dict[str, object]] = res["items"]  # type: ignore[assignment]
        return {
            "items": items_out,
            "combined_text": combined,
            "text": combined,
            "count": len(items_out),
        }

    if body.tab is None:
        return _lab_v22_error(
            400,
            "BATCH_LIMIT_EXCEEDED",
            "请提供 items 或 tab 参数",
        )

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
        .where(ImportJob.status.in_(("provisioned", "merged")))
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


def _parse_person_uuid(raw_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw_id).strip())
    except ValueError:
        return None


def _serialize_dossier_project(
    project: LabProject, relation_role: str, match_kind: str = "exact"
) -> dict[str, object]:
    base = _serialize_project(project, provenance={})
    copy_txt = str(base.get("application_text") or "")
    return {
        **base,
        "code": project.grant_no,
        "source": project.funder,
        "relation_role": relation_role,
        "match": match_kind,
        "copy_text": copy_txt,
    }


def _serialize_dossier_achievement(
    ach: LabAchievement,
    relation_role: str,
    author_position: int,
    match_kind: str = "exact",
) -> dict[str, object]:
    base = _serialize_achievement(ach, provenance={})
    copy_txt = str(base.get("application_text") or "")
    return {
        **base,
        "type": ach.kind,
        "authors": ach.authors_text or ach.first_author,
        "relation_role": relation_role,
        "author_position": author_position,
        "match": match_kind,
        "copy_text": copy_txt,
    }


@router.get("/lab/people/{person_id}/dossier", response_model=None)
async def get_person_dossier(
    person_id: str,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object] | JSONResponse:
    uid = _parse_person_uuid(person_id)
    if uid is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    person = await session.get(LabPerson, uid)
    if person is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    can_edit = bool(
        actor.role == "admin"
        or (person.user_id is not None and person.user_id == actor.id)
    )
    target_norm = normalize_dossier_name(person.name)
    ilike_pats = build_ilike_patterns(person.name)

    same_name_rows = list(
        await session.execute(
            select(LabPerson.id, LabPerson.name).where(
                or_(*[LabPerson.name.ilike(p, escape="\\") for p in ilike_pats])
            )
        )
    )
    same_name_count = sum(
        1
        for _, cand_name in same_name_rows
        if normalize_dossier_name(cand_name) == target_norm
    )
    name_ambiguous = same_name_count >= 2
    match_kind = "ambiguous" if name_ambiguous else "exact"

    proj_stmt = select(LabProject).where(
        or_(
            *[
                col.ilike(p, escape="\\")
                for p in ilike_pats
                for col in (
                    LabProject.pi_name,
                    LabProject.raw_text,
                    LabProject.relationship_note,
                )
            ]
        )
    )
    cand_projects = list(await session.scalars(proj_stmt))

    matched_proj_tuples: list[tuple[LabProject, str]] = []
    for proj in cand_projects:
        rel_role = classify_project_for_person(proj, target_norm)
        if rel_role is not None:
            matched_proj_tuples.append((proj, rel_role))

    matched_proj_tuples.sort(
        key=lambda item: (
            0 if item[1] == "pi" else 1,
            0 if item[0].start_date is not None else 1,
            -(item[0].start_date.toordinal() if item[0].start_date is not None else 0),
            -item[0].id.int,
        )
    )
    projects_total = len(matched_proj_tuples)
    pi_count = sum(1 for _, rl in matched_proj_tuples if rl == "pi")
    part_count = sum(1 for _, rl in matched_proj_tuples if rl == "participant")
    projects_truncated = projects_total > 100
    matched_proj_tuples = matched_proj_tuples[:100]
    serialized_projects = [
        _serialize_dossier_project(proj, rel_role, match_kind)
        for proj, rel_role in matched_proj_tuples
    ]

    ach_stmt = select(LabAchievement).where(
        or_(
            *[
                col.ilike(p, escape="\\")
                for p in ilike_pats
                for col in (
                    LabAchievement.first_author,
                    LabAchievement.authors_text,
                    LabAchievement.raw_text,
                )
            ]
        )
    )
    cand_achievements = list(await session.scalars(ach_stmt))

    matched_ach_tuples: list[tuple[LabAchievement, str, int]] = []
    for ach in cand_achievements:
        classified = classify_achievement_for_person(ach, target_norm)
        if classified is not None:
            matched_ach_tuples.append((ach, classified[0], classified[1]))

    matched_ach_tuples.sort(
        key=lambda item: (
            0 if item[0].year is not None else 1,
            -(item[0].year if item[0].year is not None else 0),
            0 if item[1] == "first_author" else 1,
            item[2],
            -item[0].id.int,
        )
    )
    outputs_total = len(matched_ach_tuples)
    first_count = sum(1 for _, rl, _ in matched_ach_tuples if rl == "first_author")
    co_count = sum(1 for _, rl, _ in matched_ach_tuples if rl == "coauthor")
    outputs_truncated = outputs_total > 200
    matched_ach_tuples = matched_ach_tuples[:200]
    serialized_outputs = [
        _serialize_dossier_achievement(ach, rel_role, pos, match_kind)
        for ach, rel_role, pos in matched_ach_tuples
    ]

    person_payload = _serialize_person(person, provenance={})
    person_payload["can_edit"] = can_edit
    person_payload["name_ambiguous"] = name_ambiguous

    return {
        "person": person_payload,
        "can_edit": can_edit,
        "name_ambiguous": name_ambiguous,
        "counts": {
            "projects_total": projects_total,
            "projects_as_pi": pi_count,
            "projects_as_participant": part_count,
            "outputs_total": outputs_total,
            "outputs_first_author": first_count,
            "outputs_coauthor": co_count,
            "achievements_total": outputs_total,
            "achievements_first_author": first_count,
            "achievements_coauthor": co_count,
        },
        "projects": serialized_projects,
        "outputs": serialized_outputs,
        "achievements": serialized_outputs,
        "truncated": {
            "projects": projects_truncated,
            "outputs": outputs_truncated,
            "achievements": outputs_truncated,
        },
    }


def _etag_matches(header_val: str | None, sha256_hex: str) -> bool:
    if not header_val:
        return False
    target = sha256_hex.lower()
    for raw_tag in header_val.split(","):
        tag = raw_tag.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        if tag.strip('"').lower() == target:
            return True
    return False


@router.get("/lab/people/{person_id}/signature", response_model=None)
async def download_person_signature(
    person_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    user = await _user(session, actor)
    uid = _parse_person_uuid(person_id)
    if uid is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    person = await session.scalar(
        select(LabPerson)
        .options(undefer(LabPerson.signature_data))
        .where(LabPerson.id == uid)
    )
    if person is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    if (
        person.signature_data is None
        or not person.signature_sha256
        or not person.signature_mime
    ):
        return _lab_v22_error(404, "SIGNATURE_NOT_FOUND", "该成员尚未录入电子签名")

    etag = f'"{person.signature_sha256}"'
    if _etag_matches(request.headers.get("if-none-match"), person.signature_sha256):
        await write_audit(
            settings=_settings(request),
            actor_id=user.id,
            action="lab.signature.read",
            resource_type="lab_person",
            resource_id=str(person.id),
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={
                "person_id": str(person.id),
                "sha256": person.signature_sha256,
                "channel": "single",
            },
        )
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Cache-Control": "private, max-age=0, must-revalidate",
            },
        )

    sig_bytes = bytes(person.signature_data)
    check = validate_signature_image(sig_bytes)
    if not check.ok or check.mime is None:
        return _lab_v22_error(
            500,
            "SIGNATURE_CORRUPT_IN_STORAGE",
            "存储中的电子签名数据异常",
        )

    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab.signature.read",
        resource_type="lab_person",
        resource_id=str(person.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "person_id": str(person.id),
            "sha256": check.sha256,
            "channel": "single",
        },
    )

    ext = MIME_TO_EXT.get(check.mime, "bin")
    safe_name = sanitize_filename_part(person.name)
    content_disp = build_rfc5987_content_disposition(
        f"signature-{person.id}.{ext}",
        f"{safe_name}_签名.{ext}",
    )
    headers = {
        "Content-Type": check.mime,
        "Content-Length": str(len(sig_bytes)),
        "Content-Disposition": content_disp,
        "ETag": etag,
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    return StreamingResponse(
        iter_bytes_chunks(sig_bytes),
        media_type=check.mime,
        headers=headers,
    )


@router.put("/lab/people/{person_id}/signature", response_model=None)
async def upload_person_signature(
    person_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object] | JSONResponse:
    user = await _user(session, actor)
    uid = _parse_person_uuid(person_id)
    if uid is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    person_pre = await session.scalar(
        select(LabPerson).where(LabPerson.id == uid)
    )
    if person_pre is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    if actor.role != "admin" and (
        person_pre.user_id is None or person_pre.user_id != user.id
    ):
        return _lab_v22_error(
            403,
            "FORBIDDEN",
            "仅管理员或本人可上传或替换电子签名",
        )

    if_match = request.headers.get("if-match")
    if if_match is not None and if_match.strip():
        pre_sha = person_pre.signature_sha256 or ""
        if not pre_sha or not _etag_matches(if_match, pre_sha):
            return _lab_v22_error(
                412,
                "PRECONDITION_FAILED",
                "电子签名已被其他请求更新，请刷新后重试",
            )

    max_multipart_bytes = 2_621_440  # 2.5 MiB (§4.3 Content-Length & stream cap)
    content_length_hdr = request.headers.get("content-length")
    if content_length_hdr is not None and content_length_hdr.strip():
        try:
            if int(content_length_hdr) > max_multipart_bytes:
                return _lab_v22_error(
                    413,
                    "SIGNATURE_TOO_LARGE",
                    "签名图片大小必须在 1 字节到 2 MiB 之间",
                )
        except ValueError:
            return _lab_v22_error(
                400,
                "INVALID_CONTENT_LENGTH",
                "非法的 Content-Length 请求头",
            )

    body_chunks: list[bytes] = []
    total_received = 0
    async for chunk in request.stream():
        total_received += len(chunk)
        if total_received > max_multipart_bytes:
            return _lab_v22_error(
                413,
                "SIGNATURE_TOO_LARGE",
                "签名图片大小必须在 1 字节到 2 MiB 之间",
            )
        body_chunks.append(chunk)
    request._body = b"".join(body_chunks)

    try:
        form = await request.form(
            max_files=1,
            max_fields=4,
            max_part_size=2_097_153,
        )
    except HTTPException as exc:
        if "exceeded maximum size" in str(exc.detail).lower() or exc.status_code == 413:
            return _lab_v22_error(
                413,
                "SIGNATURE_TOO_LARGE",
                "签名图片大小必须在 1 字节到 2 MiB 之间",
            )
        return _lab_v22_error(
            422,
            "SIGNATURE_FILE_MISSING",
            "未提供签名图片文件或表单格式非法",
        )

    try:
        uploaded = form.get("file")
        if uploaded is None or isinstance(uploaded, str) or not hasattr(uploaded, "read"):
            return _lab_v22_error(
                422,
                "SIGNATURE_FILE_MISSING",
                "未提供签名图片文件或文件为空",
            )
        raw_bytes = await uploaded.read(2_097_153)
    finally:
        await form.close()
    check = validate_signature_image(raw_bytes)
    if not check.ok or check.mime is None or check.sha256 is None:
        err_code = check.error_code or "SIGNATURE_CORRUPT"
        if err_code == "SIGNATURE_TOO_LARGE":
            return _lab_v22_error(
                413,
                "SIGNATURE_TOO_LARGE",
                "签名图片大小必须在 1 字节到 2 MiB 之间",
            )
        if err_code == "SIGNATURE_REJECTED_FORMAT":
            return _lab_v22_error(
                415,
                "SIGNATURE_REJECTED_FORMAT",
                "仅支持 PNG、JPEG 或 WebP 格式的电子签名图片",
            )
        return _lab_v22_error(
            422,
            err_code,
            "签名图片文件损坏或尺寸不在允许范围内（8×8 至 6000×6000）",
        )

    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    person = await session.scalar(
        select(LabPerson)
        .where(LabPerson.id == uid)
        .options(undefer(LabPerson.signature_data))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if person is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")
    if actor.role != "admin" and (person.user_id is None or person.user_id != user.id):
        return _lab_v22_error(
            403,
            "FORBIDDEN",
            "仅管理员或本人可上传或替换电子签名",
        )
    if if_match is not None and if_match.strip():
        cur_sha = person.signature_sha256 or ""
        if not cur_sha or not _etag_matches(if_match, cur_sha):
            return _lab_v22_error(
                412,
                "PRECONDITION_FAILED",
                "电子签名已被其他请求更新，请刷新后重试",
            )

    now_utc = datetime.now(UTC)
    person.signature_data = raw_bytes
    person.signature_mime = check.mime
    person.signature_sha256 = check.sha256
    person.signature_updated_at = now_utc
    person.signature_source = "upload"
    await session.flush()
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab.signature.upload",
        resource_type="lab_person",
        resource_id=str(person.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "person_id": str(person.id),
            "sha256": check.sha256,
            "size": check.size,
            "mime": check.mime,
            "width": check.width,
            "height": check.height,
        },
    )
    return {
        "person_id": str(person.id),
        "has_signature": True,
        "signature_mime": check.mime,
        "signature_sha256": check.sha256,
        "signature_updated_at": now_utc.isoformat(),
        "signature_source": "upload",
        "signature_url": f"/api/v1/lab/people/{person.id}/signature",
        "width": check.width,
        "height": check.height,
        "size": check.size,
    }


@router.delete("/lab/people/{person_id}/signature", response_model=None)
async def delete_person_signature(
    person_id: str,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object] | JSONResponse:
    user = await _user(session, actor)
    uid = _parse_person_uuid(person_id)
    if uid is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    person = await session.scalar(
        select(LabPerson)
        .options(undefer(LabPerson.signature_data))
        .where(LabPerson.id == uid)
        .with_for_update()
    )
    if person is None:
        return _lab_v22_error(404, "NOT_FOUND", "成员档案不存在")

    if actor.role != "admin" and (person.user_id is None or person.user_id != user.id):
        return _lab_v22_error(
            403,
            "FORBIDDEN",
            "仅管理员或本人可删除电子签名",
        )

    if_match = request.headers.get("if-match")
    if if_match is not None and if_match.strip():
        cur_sha = person.signature_sha256 or ""
        if not cur_sha or not _etag_matches(if_match, cur_sha):
            return _lab_v22_error(
                412,
                "PRECONDITION_FAILED",
                "电子签名已被其他请求更新，请刷新后重试",
            )

    prev_sha = person.signature_sha256
    person.signature_data = None
    person.signature_mime = None
    person.signature_sha256 = None
    person.signature_updated_at = None
    person.signature_source = None
    await session.flush()
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab.signature.delete",
        resource_type="lab_person",
        resource_id=str(person.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "person_id": str(person.id),
            "previous_sha256": prev_sha,
        },
    )
    return {
        "person_id": str(person.id),
        "has_signature": False,
        "signature_mime": None,
        "signature_sha256": None,
        "signature_updated_at": None,
        "signature_source": None,
        "signature_url": None,
    }


@router.post("/lab/people/signatures:zip", response_model=None)
async def download_people_signatures_zip(
    body: SignaturesZipBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    user = await _user(session, actor)
    try:
        (
            zip_bytes,
            included_count,
            missing_count,
            included_signatures,
        ) = await prepare_signatures_zip(
            session,
            raw_person_ids=body.person_ids,
            filename_template=body.filename_template,
        )
    except BasketError as exc:
        return _lab_v22_error(exc.status, exc.code, exc.message)

    for sig_item in included_signatures:
        await write_audit(
            settings=_settings(request),
            actor_id=user.id,
            action="lab.signature.read",
            resource_type="lab_person",
            resource_id=sig_item["person_id"],
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={
                "person_id": sig_item["person_id"],
                "sha256": sig_item["sha256"],
                "channel": "zip",
            },
        )

    content_disp = build_rfc5987_content_disposition(
        "lab-signatures.zip",
        "项目组签名.zip",
    )
    headers = {
        "Content-Disposition": content_disp,
        "Cache-Control": "no-store",
        "X-Signature-Count-Included": str(included_count),
        "X-Signature-Count-Missing": str(missing_count),
    }
    return StreamingResponse(
        iter_bytes_chunks(zip_bytes),
        media_type="application/zip",
        headers=headers,
    )


@router.post("/lab/export:selected", response_model=None)
async def export_selected_basket_docx(
    body: ExportSelectedBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Response:
    user = await _user(session, actor)
    raw_items = [{"entity": it.entity, "id": it.id} for it in body.items]
    opts = body.options
    try:
        docx_bytes, warnings_count, embedded_signatures = await build_selected_docx(
            session,
            title=body.title,
            raw_items=raw_items,
            style=opts.style,
            numbering=opts.numbering,
            group_by_entity=opts.group_by_entity,
            include_person_bio=opts.include_person_bio,
            include_support_note=opts.include_support_note,
            include_signatures_annex=opts.include_signatures_annex,
        )
    except BasketError as exc:
        return _lab_v22_error(
            exc.status,
            exc.code,
            exc.message,
            missing=exc.missing,
        )

    for sig_item in embedded_signatures:
        await write_audit(
            settings=_settings(request),
            actor_id=user.id,
            action="lab.signature.read",
            resource_type="lab_person",
            resource_id=sig_item["person_id"],
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={
                "person_id": sig_item["person_id"],
                "sha256": sig_item["sha256"],
                "channel": "docx_export",
            },
        )

    doc_title_safe = sanitize_filename_part(body.title or "课题申报素材汇编")
    content_disp = build_rfc5987_content_disposition(
        "proposal-materials.docx",
        f"{doc_title_safe}.docx",
    )
    headers = {
        "Content-Disposition": content_disp,
        "Cache-Control": "no-store",
        "X-Export-Warnings-Count": str(warnings_count),
    }
    return StreamingResponse(
        iter_bytes_chunks(docx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )

