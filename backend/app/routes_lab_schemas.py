"""Shared Pydantic schemas, serializers, and auth helpers for /api/v1/admin/imports and /api/v1/lab."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Actor
from app.importer.fingerprint import project_fp
from app.importer.formatters import (
    ACHIEVEMENT_KIND_LABELS,
    PROJECT_STATUS_LABELS,
    format_achievement_application,
    format_facility_application,
    format_person_application,
    format_project_application,
)
from app.importer.people import normalize_person_name
from app.importer.pii import scrub_free_text
from app.importer.provenance import (
    fallback_entity_provenance_summary,
)
from app.importer.provision import (
    _parse_iso_date,
)
from app.models import (
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
    User,
)
from app.routes import APIError, _user


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
    include_relationship_note: bool | None = None
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


