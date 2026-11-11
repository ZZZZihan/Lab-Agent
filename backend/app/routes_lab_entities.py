"""API routes for Lab Achievements, Facilities, and Field Provenance Restoration (/api/v1/lab)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.fingerprint import achievement_fp, facility_fp
from app.importer.formatters import (
    ACHIEVEMENT_KIND_LABELS,
)
from app.importer.provenance import (
    ACHIEVEMENT_MANAGED_FIELDS,
    FACILITY_MANAGED_FIELDS,
    build_provenance_map,
    delete_entity_provenance,
    get_entity_json_value,
    record_entity_import_provenance,
    record_manual_field_edit,
    restore_entity_imported_fields,
)
from app.importer.provision import (
    ProvisionError,
    _parse_decimal,
    _parse_iso_date,
    sync_lab_profile_rag,
)
from app.models import (
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
    User,
)
from app.routes import APIError, _actor, _client_ip, _embedder, _settings, _user
from app.routes_admin import _require_admin
from app.routes_lab_schemas import (
    AchievementBody,
    FacilityBody,
    RestoreImportedBody,
    _assert_outbound_no_pii,
    _require_lab_editor,
    _resolve_source_import_id,
    _scrub_str,
    _serialize_achievement,
    _serialize_facility,
    _serialize_person,
    _serialize_project,
)

router = APIRouter()


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
    _assert_outbound_no_pii(serialized_ach)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_achievement_created",
        resource_type="lab_achievement",
        resource_id=str(achievement.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "kind": achievement.kind,
            "year": achievement.year,
            "rag_doc_key": doc_key,
        },
        session=session,
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
    _assert_outbound_no_pii(serialized_ach)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_achievement_updated",
        resource_type="lab_achievement",
        resource_id=str(achievement.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"fields": list(touched_fields), "rag_doc_key": doc_key},
        session=session,
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

    await delete_entity_provenance(session, "achievement", achievement_id)
    await session.delete(achievement)
    await session.flush()
    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_achievement_deleted",
        resource_type="lab_achievement",
        resource_id=str(achievement_id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"rag_doc_key": doc_key},
        session=session,
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
    _assert_outbound_no_pii(serialized_fac)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_facility_created",
        resource_type="lab_facility",
        resource_id=str(facility.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"category": facility.category, "rag_doc_key": doc_key},
        session=session,
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
    _assert_outbound_no_pii(serialized_fac)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_facility_updated",
        resource_type="lab_facility",
        resource_id=str(facility.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"fields": list(touched_fields), "rag_doc_key": doc_key},
        session=session,
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
    _assert_outbound_no_pii(serialized)

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_field_restored_imported",
        resource_type=f"lab_{norm_type}",
        resource_id=str(entity.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"fields": restored, "rag_doc_key": doc_key},
        session=session,
    )
    await session.commit()
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

    await delete_entity_provenance(session, "facility", facility_id)
    await session.delete(facility)
    await session.flush()
    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=_embedder(request),
        actor_id=user.id,
    )
    await write_audit(
        settings=_settings(request),
        actor_id=user.id,
        action="lab_facility_deleted",
        resource_type="lab_facility",
        resource_id=str(facility_id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"rag_doc_key": doc_key},
        session=session,
    )
    await session.commit()
    return {"status": "deleted", "id": str(facility_id), "rag_doc_key": doc_key}


