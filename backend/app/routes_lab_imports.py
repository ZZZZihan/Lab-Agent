"""API routes for Proposal Cold-Start & Phase 2 Import Jobs (/api/v1/admin/imports)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.fingerprint import achievement_fp, facility_fp
from app.importer.parser import parse_proposal_docx, reparse_preview_payload
from app.importer.people import assign_people_identities
from app.importer.pii import PIILeakError, assert_no_pii
from app.importer.provenance import (
    MergePreviewRequest,
    MergeRequest,
    backfill_field_provenance,
    build_merge_preview,
    execute_merge,
    revert_import_merge,
)
from app.importer.provision import (
    ProvisionError,
    ProvisionRequest,
    _parse_iso_date,
    build_job_roster,
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
from app.routes import APIError, _actor, _client_ip, _embedder, _settings
from app.routes_admin import _require_admin
from app.routes_lab_schemas import (
    ReparseImportBody,
    UpdateImportPreviewBody,
    _recompute_inferred_projects,
    _scrub_obj,
    _scrub_str,
    _serialize_import_job,
)

router = APIRouter()


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
                p["cert"] = p.get("student_id") or p.get("login_name")
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


