"""API routes for Lab Assets Hub (/api/v1/lab) and aggregated lab sub-routers."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.fingerprint import person_fp, project_fp
from app.importer.people import clean_email, normalize_person_name
from app.importer.pii import assert_no_pii
from app.importer.provenance import (
    PERSON_MANAGED_FIELDS,
    PROJECT_MANAGED_FIELDS,
    build_provenance_map,
    get_entity_json_value,
    record_entity_import_provenance,
    record_manual_field_edit,
)
from app.importer.provision import (
    _parse_decimal,
    _parse_iso_date,
    sync_lab_profile_rag,
)
from app.models import (
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
    User,
)
from app.routes import APIError, _actor, _client_ip, _embedder, _settings, _user
from app.routes_lab_basket import router as basket_router
from app.routes_lab_dossier import router as dossier_router
from app.routes_lab_entities import router as entities_router
from app.routes_lab_import_batch import router as import_batch_router
from app.routes_lab_imports import router as imports_router
from app.routes_lab_schemas import (
    ProjectBody,
    UpdatePersonBody,
    _require_lab_editor,
    _resolve_source_import_id,
    _scrub_str,
    _serialize_achievement,
    _serialize_facility,
    _serialize_import_job,
    _serialize_person,
    _serialize_project,
)

router = APIRouter(prefix="/api/v1")
router.include_router(import_batch_router)
router.include_router(imports_router)


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
    rag_doc_id = str(rag_doc.id) if rag_doc is not None else None
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_profile_reindexed",
        resource_type="document",
        resource_id=rag_doc_id,
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"rag_doc_key": doc_key},
        session=session,
    )
    await session.commit()
    return {
        "status": "ok",
        "rag_doc_key": doc_key,
        "rag_document_id": rag_doc_id,
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
    audited_fields = (["name"] if "name" in fields_set else []) + list(touched_fields)
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_person_updated",
        resource_type="lab_person",
        resource_id=str(person.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"fields": audited_fields, "rag_doc_key": doc_key},
        session=session,
    )
    await session.commit()
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
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_project_created",
        resource_type="lab_project",
        resource_id=str(project.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "status": project.status,
            "lab_role": project.lab_role,
            "rag_doc_key": doc_key,
        },
        session=session,
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
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_project_updated",
        resource_type="lab_project",
        resource_id=str(project.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"fields": list(touched_fields), "rag_doc_key": doc_key},
        session=session,
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
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_project_deleted",
        resource_type="lab_project",
        resource_id=str(project_id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"rag_doc_key": doc_key},
        session=session,
    )
    await session.commit()
    return {"status": "deleted", "id": str(project_id), "rag_doc_key": doc_key}



router.include_router(entities_router)
router.include_router(basket_router)
router.include_router(dossier_router)
