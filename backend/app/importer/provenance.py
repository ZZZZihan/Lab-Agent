"""Phase 2 field-level provenance tracking, temporal merge arbitration, backfill, and revert (Spec §10)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.importer.fingerprint import (
    achievement_fp,
    facility_fp,
    norm,
    person_fp,
    project_fp,
)
from app.importer.people import (
    assign_people_identities,
    clean_email,
    normalize_person_name,
)
from app.importer.provision import (
    ProvisionError,
    _parse_decimal,
    _parse_iso_date,
    sync_lab_profile_rag,
)
from app.models import (
    FieldProvenance,
    ImportJob,
    LabAchievement,
    LabFacility,
    LabPerson,
    LabProject,
    User,
)
from app.rag.embedder import Embedder

EntityType = Literal["person", "project", "achievement", "facility"]
MergeCategory = Literal[
    "added",
    "updated",
    "fill_blank",
    "time_regression",
    "manual_protected",
    "same_time_conflict",
]
MergeAction = Literal["adopt_incoming", "keep_current", "force_adopt"]

PERSON_MANAGED_FIELDS: tuple[str, ...] = (
    "title",
    "admin_duty",
    "tutor_role",
    "research_specialty",
    "degree",
    "affiliation",
    "email",
    "project_role",
    "bio",
)

PROJECT_MANAGED_FIELDS: tuple[str, ...] = (
    "title",
    "funder",
    "grant_no",
    "pi_name",
    "lab_role",
    "start_date",
    "end_date",
    "amount_wan",
    "status",
    "status_source",
    "relationship_note",
    "raw_text",
)

ACHIEVEMENT_MANAGED_FIELDS: tuple[str, ...] = (
    "kind",
    "title",
    "first_author",
    "authors_text",
    "venue",
    "year",
    "support_note",
    "raw_text",
    "parse_quality",
)

FACILITY_MANAGED_FIELDS: tuple[str, ...] = (
    "category",
    "name",
    "spec",
    "value_wan",
    "usage_scenario",
    "raw_text",
)

MANAGED_FIELDS: dict[str, tuple[str, ...]] = {
    "person": PERSON_MANAGED_FIELDS,
    "project": PROJECT_MANAGED_FIELDS,
    "achievement": ACHIEVEMENT_MANAGED_FIELDS,
    "facility": FACILITY_MANAGED_FIELDS,
}


class MergePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of_time: date | None = None


class MergeDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_key: str
    action: MergeAction


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of_time: date | None = None
    decisions: list[MergeDecision] = Field(default_factory=list)


def _kind_rank(source_kind: str) -> int:
    if source_kind == "manual":
        return 3
    if source_kind == "import":
        return 2
    if source_kind == "legacy":
        return 1
    return 0


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _epoch_utc(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.min.replace(tzinfo=UTC)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def effective_value(entries: Sequence[FieldProvenance]) -> FieldProvenance | None:
    """Compute the effective provenance record for a single field (Spec §10.4)."""
    live = [e for e in entries if not e.superseded]
    if not live:
        return None
    manual = [e for e in live if e.source_kind == "manual"]
    if manual:
        return max(manual, key=lambda e: (_epoch_utc(e.recorded_at), e.id or 0))
    candidates = [e for e in live if not _is_blank(e.value)]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda e: (
            e.as_of_time,
            _kind_rank(e.source_kind),
            _epoch_utc(e.recorded_at),
            e.id or 0,
        ),
    )


def normalize_field_json_value(entity_type: str, field: str, raw: object) -> object:
    """Normalize an entity attribute or preview value into canonical JSON scalar form."""
    if raw is None:
        return None
    if field in ("start_date", "end_date"):
        d = _parse_iso_date(raw)
        return d.isoformat() if d is not None else None
    if field in ("amount_wan", "value_wan"):
        dec = raw if isinstance(raw, Decimal) else _parse_decimal(raw)
        if dec is None:
            return None
        try:
            return str(dec.quantize(Decimal("0.01")))
        except InvalidOperation:
            return str(dec)
    if field == "year":
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw
        s = str(raw).strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return None
    if field == "email":
        return clean_email(str(raw))
    s_val = str(raw).strip()
    if not s_val:
        return None
    return s_val


def get_entity_json_value(entity_type: str, entity: Any, field: str) -> object:
    """Read and normalize a field value from an ORM entity instance."""
    return normalize_field_json_value(entity_type, field, getattr(entity, field, None))


def set_entity_field_from_json(
    entity_type: str, entity: Any, field: str, json_val: object
) -> None:
    """Write a canonical JSON value back onto an ORM entity instance."""
    norm_val = normalize_field_json_value(entity_type, field, json_val)
    if field in ("start_date", "end_date"):
        setattr(entity, field, _parse_iso_date(norm_val))
        return
    if field in ("amount_wan", "value_wan"):
        setattr(entity, field, _parse_decimal(norm_val))
        return
    if field == "year":
        setattr(entity, field, norm_val if isinstance(norm_val, int) else None)
        return

    # Non-nullable string columns with defaults
    if entity_type == "person" and field == "project_role":
        setattr(
            entity,
            field,
            norm_val if norm_val in ("applicant", "member") else "member",
        )
        return
    if entity_type == "project":
        if field == "title":
            if isinstance(norm_val, str) and norm_val:
                entity.title = norm_val
            return
        if field == "lab_role":
            entity.lab_role = (
                norm_val
                if norm_val in ("host", "participant", "unknown")
                else "host"
            )
            return
        if field == "status":
            entity.status = (
                norm_val
                if norm_val in ("ongoing", "completed", "unknown")
                else "unknown"
            )
            return
        if field == "status_source":
            entity.status_source = (
                norm_val if norm_val in ("explicit", "inferred") else "explicit"
            )
            return
        if field == "raw_text":
            entity.raw_text = (
                norm_val
                if isinstance(norm_val, str) and norm_val
                else entity.title
            )
            return
    if entity_type == "achievement":
        if field == "kind":
            entity.kind = str(norm_val) if norm_val else "other"
            return
        if field == "title":
            if isinstance(norm_val, str) and norm_val:
                entity.title = norm_val
            return
        if field == "raw_text":
            entity.raw_text = (
                norm_val
                if isinstance(norm_val, str) and norm_val
                else entity.title
            )
            return
        if field == "parse_quality":
            entity.parse_quality = (
                norm_val if norm_val in ("structured", "raw") else "structured"
            )
            return
    if entity_type == "facility":
        if field == "category":
            entity.category = str(norm_val) if norm_val else "equipment"
            return
        if field == "name":
            if isinstance(norm_val, str) and norm_val:
                entity.name = norm_val
            return
        if field == "raw_text":
            entity.raw_text = (
                norm_val
                if isinstance(norm_val, str) and norm_val
                else entity.name
            )
            return

    setattr(entity, field, norm_val)


async def record_entity_import_provenance(
    session: AsyncSession,
    *,
    entity_type: EntityType,
    entity: Any,
    job_id: uuid.UUID | None,
    as_of_time: date,
    recorded_by: uuid.UUID | None,
    source_kind: Literal["import", "manual", "legacy"] = "import",
) -> int:
    """Write initial provenance rows for all non-blank managed fields."""
    count = 0
    for field in MANAGED_FIELDS[entity_type]:
        val = get_entity_json_value(entity_type, entity, field)
        if _is_blank(val):
            continue
        session.add(
            FieldProvenance(
                entity_type=entity_type,
                entity_id=entity.id,
                field=field,
                value=val,
                source_kind=source_kind,
                source_job_id=None if source_kind == "manual" else job_id,
                as_of_time=as_of_time,
                recorded_by=recorded_by,
                superseded=False,
            )
        )
        count += 1
    return count


async def record_manual_field_edit(
    session: AsyncSession,
    *,
    entity_type: EntityType,
    entity: Any,
    field: str,
    old_val: object,
    new_val: object,
    actor_id: uuid.UUID,
    force_record: bool = False,
) -> None:
    """Record a `source_kind='manual'` edit, backfilling baseline import provenance if absent."""
    if field not in MANAGED_FIELDS[entity_type]:
        return
    norm_old = normalize_field_json_value(entity_type, field, old_val)
    norm_new = normalize_field_json_value(entity_type, field, new_val)
    if norm_old == norm_new and not force_record:
        return

    existing_rows = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_type == entity_type,
                FieldProvenance.entity_id == entity.id,
                FieldProvenance.field == field,
            )
        )
    )
    if not existing_rows and not _is_blank(norm_old):
        src_job_id = getattr(entity, "source_import_id", None)
        base_as_of = getattr(entity, "as_of_time", None) or date.today()
        session.add(
            FieldProvenance(
                entity_type=entity_type,
                entity_id=entity.id,
                field=field,
                value=norm_old,
                source_kind="import" if src_job_id is not None else "legacy",
                source_job_id=src_job_id,
                as_of_time=base_as_of,
                recorded_by=actor_id,
                superseded=False,
            )
        )

    session.add(
        FieldProvenance(
            entity_type=entity_type,
            entity_id=entity.id,
            field=field,
            value=norm_new,
            source_kind="manual",
            source_job_id=None,
            as_of_time=date.today(),
            recorded_by=actor_id,
            superseded=False,
        )
    )


async def _refresh_and_validate_entity_fingerprint(
    session: AsyncSession,
    entity_type: EntityType,
    entity: Any,
    *,
    prior_affiliation: str | None = None,
    prior_email: str | None = None,
) -> None:
    """Recompute and validate `entity_fingerprint` after provenance-driven field mutations."""
    new_fp: str
    if entity_type == "person":
        t1_fp: str | None = None
        if getattr(entity, "user_id", None) is not None:
            linked_user = await session.get(User, entity.user_id)
            if linked_user is not None and linked_user.login_name:
                cand_t1 = person_fp(
                    tier="T1",
                    login_name=linked_user.login_name,
                    name=entity.name,
                    affiliation=entity.affiliation,
                    email=entity.email,
                )
                if cand_t1 == entity.entity_fingerprint:
                    t1_fp = cand_t1
        candidate_affiliations: set[str | None] = {
            None,
            entity.affiliation,
            prior_affiliation,
        }
        candidate_emails: set[str | None] = {
            None,
            entity.email,
            prior_email,
        }
        hist_rows = list(
            await session.scalars(
                select(FieldProvenance).where(
                    FieldProvenance.entity_type == "person",
                    FieldProvenance.entity_id == entity.id,
                    FieldProvenance.field.in_(("affiliation", "email")),
                )
            )
        )
        for r in hist_rows:
            if isinstance(r.value, str) and r.value.strip():
                if r.field == "affiliation":
                    candidate_affiliations.add(r.value.strip())
                elif r.field == "email":
                    candidate_emails.add(r.value.strip())
        non_t1_fps = {
            person_fp(
                tier="T3" if (em and em.strip()) else "T2",
                login_name=None,
                name=entity.name,
                affiliation=aff,
                email=em,
            )
            for aff in candidate_affiliations
            for em in candidate_emails
        }
        if t1_fp is not None:
            new_fp = t1_fp
        elif entity.entity_fingerprint not in non_t1_fps:
            new_fp = entity.entity_fingerprint
        else:
            new_fp = person_fp(
                tier="T3" if entity.email else "T2",
                login_name=None,
                name=entity.name,
                affiliation=entity.affiliation,
                email=entity.email,
            )
    elif entity_type == "project":
        new_fp = project_fp(
            grant_no=entity.grant_no,
            title=entity.title,
            funder=entity.funder,
        )
    elif entity_type == "achievement":
        new_fp = achievement_fp(
            kind=entity.kind,
            title=entity.title,
            year=entity.year,
            raw_text=entity.raw_text or entity.title,
            parse_quality=entity.parse_quality or "structured",
        )
    else:
        new_fp = facility_fp(
            category=entity.category,
            name=entity.name,
            spec=entity.spec,
        )

    if new_fp != entity.entity_fingerprint:
        model_map: dict[EntityType, Any] = {
            "person": LabPerson,
            "project": LabProject,
            "achievement": LabAchievement,
            "facility": LabFacility,
        }
        model_cls = model_map[entity_type]
        dup_id = await session.scalar(
            select(model_cls.id).where(
                model_cls.entity_fingerprint == new_fp,
                model_cls.id != entity.id,
            )
        )
        if dup_id is not None:
            raise ProvisionError(
                409,
                "duplicate_entity",
                "恢复或合并后的记录与现有资产指纹冲突",
            )
        entity.entity_fingerprint = new_fp


async def restore_entity_imported_fields(
    session: AsyncSession,
    *,
    entity_type: EntityType,
    entity: Any,
    fields: Sequence[str],
) -> list[str]:
    """Supersede active manual overrides on `fields` and restore effective imported values."""
    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    prior_affiliation = getattr(entity, "affiliation", None)
    prior_email = getattr(entity, "email", None)
    target_fields = list(fields)
    if (
        entity_type == "project"
        and "status" in target_fields
        and "status_source" not in target_fields
    ):
        target_fields.append("status_source")
    restored: list[str] = []
    for field in target_fields:
        if field not in MANAGED_FIELDS[entity_type]:
            raise ProvisionError(422, "invalid_field", f"不支持恢复该字段: {field}")
        rows = list(
            await session.scalars(
                select(FieldProvenance).where(
                    FieldProvenance.entity_type == entity_type,
                    FieldProvenance.entity_id == entity.id,
                    FieldProvenance.field == field,
                    FieldProvenance.superseded.is_(False),
                )
            )
        )
        if not rows:
            cur_val = get_entity_json_value(entity_type, entity, field)
            if not _is_blank(cur_val):
                src_job_id = getattr(entity, "source_import_id", None)
                base_as_of = getattr(entity, "as_of_time", None) or date.today()
                session.add(
                    FieldProvenance(
                        entity_type=entity_type,
                        entity_id=entity.id,
                        field=field,
                        value=cur_val,
                        source_kind="import" if src_job_id is not None else "legacy",
                        source_job_id=src_job_id,
                        as_of_time=base_as_of,
                        recorded_by=None,
                        superseded=False,
                    )
                )
            restored.append(field)
            continue
        manual_rows = [r for r in rows if r.source_kind == "manual"]
        for mr in manual_rows:
            mr.superseded = True
        remaining = [r for r in rows if r.source_kind != "manual"]
        if not manual_rows and not remaining:
            restored.append(field)
            continue
        eff = effective_value(remaining)
        set_entity_field_from_json(
            entity_type,
            entity,
            field,
            eff.value if eff is not None else None,
        )
        restored.append(field)

    await _refresh_and_validate_entity_fingerprint(
        session,
        entity_type,
        entity,
        prior_affiliation=prior_affiliation,
        prior_email=prior_email,
    )
    await session.flush()
    all_live = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_type == entity_type,
                FieldProvenance.entity_id == entity.id,
                FieldProvenance.superseded.is_(False),
            )
        )
    )
    non_manual_dates = [
        r.as_of_time
        for r in all_live
        if r.source_kind != "manual" and r.as_of_time is not None
    ]
    if non_manual_dates:
        entity.as_of_time = max(non_manual_dates)
    await session.flush()
    return restored


def fallback_entity_provenance_summary(
    entity_type: EntityType, entity: Any
) -> dict[str, dict[str, object]]:
    """Build fallback field provenance summary from entity columns when no DB rows are loaded."""
    src_job_id = getattr(entity, "source_import_id", None)
    as_of = getattr(entity, "as_of_time", None)
    as_of_str = as_of.isoformat() if isinstance(as_of, date) else None
    src_kind = "import" if src_job_id is not None else "legacy"
    summary: dict[str, dict[str, object]] = {}
    for field in MANAGED_FIELDS[entity_type]:
        val = get_entity_json_value(entity_type, entity, field)
        if _is_blank(val):
            continue
        summary[field] = {
            "source_kind": src_kind,
            "as_of_time": as_of_str,
            "source_job_id": str(src_job_id) if src_job_id is not None else None,
            "has_manual_override": False,
            "imported_value": val,
        }
    return summary


async def build_provenance_map(
    session: AsyncSession,
    entity_type: EntityType,
    entities: Sequence[Any],
) -> dict[uuid.UUID, dict[str, dict[str, object]]]:
    """Batch-compute per-field provenance summaries for a list of entities (Spec §10.8)."""
    if not entities:
        return {}
    entity_ids = [e.id for e in entities if getattr(e, "id", None) is not None]
    rows = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.entity_type == entity_type,
                FieldProvenance.entity_id.in_(entity_ids),
                FieldProvenance.superseded.is_(False),
            )
        )
    )
    by_entity_field: dict[tuple[uuid.UUID, str], list[FieldProvenance]] = {}
    for r in rows:
        by_entity_field.setdefault((r.entity_id, r.field), []).append(r)

    result: dict[uuid.UUID, dict[str, dict[str, object]]] = {}
    for ent in entities:
        ent_summary: dict[str, dict[str, object]] = {}
        src_job_id = getattr(ent, "source_import_id", None)
        ent_as_of = getattr(ent, "as_of_time", None)
        ent_as_of_str = ent_as_of.isoformat() if isinstance(ent_as_of, date) else None
        for field in MANAGED_FIELDS[entity_type]:
            cur_val = get_entity_json_value(entity_type, ent, field)
            f_rows = by_entity_field.get((ent.id, field), [])
            if f_rows:
                eff = effective_value(f_rows)
                non_manual = [r for r in f_rows if r.source_kind != "manual"]
                best_imp = effective_value(non_manual)
                if eff is not None:
                    has_manual = eff.source_kind == "manual"
                    imp_val = best_imp.value if best_imp is not None else None
                    ent_summary[field] = {
                        "source_kind": eff.source_kind,
                        "as_of_time": (
                            eff.as_of_time.isoformat() if eff.as_of_time else ent_as_of_str
                        ),
                        "source_job_id": (
                            str(eff.source_job_id)
                            if eff.source_job_id is not None
                            else (
                                str(best_imp.source_job_id)
                                if best_imp is not None and best_imp.source_job_id is not None
                                else None
                            )
                        ),
                        "has_manual_override": has_manual,
                        "imported_value": imp_val,
                    }
            elif not _is_blank(cur_val):
                ent_summary[field] = {
                    "source_kind": "import" if src_job_id is not None else "legacy",
                    "as_of_time": ent_as_of_str,
                    "source_job_id": str(src_job_id) if src_job_id is not None else None,
                    "has_manual_override": False,
                    "imported_value": cur_val,
                }
        result[ent.id] = ent_summary
    return result


def _extract_person_incoming(
    person_dict: dict[str, Any],
) -> tuple[str, str, dict[str, object]]:
    pname = normalize_person_name(str(person_dict.get("name") or ""))
    p_email = clean_email(str(person_dict.get("email") or ""))
    p_login = str(person_dict.get("login_name") or "").strip().lower() or None
    p_tier = str(person_dict.get("tier") or "T2")
    p_aff = str(person_dict.get("affiliation") or "").strip() or None
    fp_val = str(person_dict.get("fingerprint") or "") or person_fp(
        tier=p_tier,
        login_name=p_login,
        name=pname,
        affiliation=p_aff,
        email=p_email,
    )
    proj_role = "applicant" if person_dict.get("project_role") == "applicant" else "member"
    fields: dict[str, object] = {
        "title": normalize_field_json_value("person", "title", person_dict.get("title")),
        "admin_duty": normalize_field_json_value(
            "person", "admin_duty", person_dict.get("admin_duty")
        ),
        "tutor_role": normalize_field_json_value(
            "person", "tutor_role", person_dict.get("tutor_role")
        ),
        "research_specialty": normalize_field_json_value(
            "person", "research_specialty", person_dict.get("research_specialty")
        ),
        "degree": normalize_field_json_value("person", "degree", person_dict.get("degree")),
        "affiliation": normalize_field_json_value("person", "affiliation", p_aff),
        "email": p_email,
        "project_role": proj_role,
        "bio": normalize_field_json_value("person", "bio", person_dict.get("bio")),
    }
    return pname, fp_val, fields


def _extract_project_incoming(
    proj_dict: dict[str, Any], as_of_date: date
) -> tuple[str, str, dict[str, object]]:
    title = str(proj_dict.get("title") or "").strip()
    funder = str(proj_dict.get("funder") or "").strip() or None
    grant_no = str(proj_dict.get("grant_no") or "").strip() or None
    pi_name = str(proj_dict.get("pi_name") or "").strip() or None
    lab_role_raw = str(proj_dict.get("lab_role") or "").strip().lower()
    lab_role: str | None = (
        lab_role_raw if lab_role_raw in ("host", "participant") else None
    )
    start_d = _parse_iso_date(proj_dict.get("start_date"))
    end_d = _parse_iso_date(proj_dict.get("end_date"))
    amt_wan = _parse_decimal(proj_dict.get("amount_wan"))
    status = str(proj_dict.get("status") or "unknown").strip()
    status_source = str(proj_dict.get("status_source") or "explicit").strip()
    if status_source == "inferred" and end_d is not None:
        status = "completed" if end_d < as_of_date else "ongoing"
    is_uninferable_status = status == "unknown" and status_source == "inferred"
    raw_text = str(proj_dict.get("raw_text") or title).strip()
    fp_val = str(proj_dict.get("fingerprint") or "") or project_fp(
        grant_no=grant_no,
        title=title,
        funder=funder,
    )
    fields: dict[str, object] = {
        "title": normalize_field_json_value("project", "title", title),
        "funder": normalize_field_json_value("project", "funder", funder),
        "grant_no": normalize_field_json_value("project", "grant_no", grant_no),
        "pi_name": normalize_field_json_value("project", "pi_name", pi_name),
        "lab_role": lab_role,
        "start_date": start_d.isoformat() if start_d is not None else None,
        "end_date": end_d.isoformat() if end_d is not None else None,
        "amount_wan": normalize_field_json_value("project", "amount_wan", amt_wan),
        "status": None if is_uninferable_status else status,
        "status_source": None if is_uninferable_status else status_source,
        "relationship_note": normalize_field_json_value(
            "project", "relationship_note", proj_dict.get("relationship_note")
        ),
        "raw_text": normalize_field_json_value("project", "raw_text", raw_text),
    }
    return title, fp_val, fields


def _extract_achievement_incoming(
    ach_dict: dict[str, Any],
) -> tuple[str, str, dict[str, object]]:
    kind = str(ach_dict.get("kind") or "other").strip()
    title = str(ach_dict.get("title") or "").strip()
    raw_text = str(ach_dict.get("raw_text") or title).strip()
    year_val = normalize_field_json_value("achievement", "year", ach_dict.get("year"))
    yr_int = year_val if isinstance(year_val, int) else None
    pq = str(ach_dict.get("parse_quality") or "structured").strip()
    if pq not in ("structured", "raw"):
        pq = "structured"
    fp_val = str(ach_dict.get("fingerprint") or "") or achievement_fp(
        kind=kind,
        title=title,
        year=yr_int,
        raw_text=raw_text,
        parse_quality=pq,
    )
    fields: dict[str, object] = {
        "kind": kind,
        "title": normalize_field_json_value("achievement", "title", title),
        "first_author": normalize_field_json_value(
            "achievement", "first_author", ach_dict.get("first_author")
        ),
        "authors_text": normalize_field_json_value(
            "achievement", "authors_text", ach_dict.get("authors_text")
        ),
        "venue": normalize_field_json_value("achievement", "venue", ach_dict.get("venue")),
        "year": yr_int,
        "support_note": normalize_field_json_value(
            "achievement", "support_note", ach_dict.get("support_note")
        ),
        "raw_text": normalize_field_json_value("achievement", "raw_text", raw_text),
        "parse_quality": pq,
    }
    return title, fp_val, fields


def _extract_facility_incoming(
    fac_dict: dict[str, Any],
) -> tuple[str, str, dict[str, object]]:
    category = str(fac_dict.get("category") or "equipment").strip()
    name = str(fac_dict.get("name") or "").strip()
    spec = str(fac_dict.get("spec") or "").strip() or None
    val_wan = _parse_decimal(fac_dict.get("value_wan"))
    usage = str(fac_dict.get("usage_scenario") or "").strip() or None
    raw_text = str(fac_dict.get("raw_text") or name).strip()
    fp_val = str(fac_dict.get("fingerprint") or "") or facility_fp(
        category=category,
        name=name,
        spec=spec,
    )
    fields: dict[str, object] = {
        "category": category,
        "name": normalize_field_json_value("facility", "name", name),
        "spec": normalize_field_json_value("facility", "spec", spec),
        "value_wan": normalize_field_json_value("facility", "value_wan", val_wan),
        "usage_scenario": normalize_field_json_value("facility", "usage_scenario", usage),
        "raw_text": normalize_field_json_value("facility", "raw_text", raw_text),
    }
    return name, fp_val, fields


def _has_login_conflict(person_dict: dict[str, Any]) -> bool:
    raw_match = person_dict.get("match")
    if isinstance(raw_match, dict) and raw_match.get("status") == "conflict":
        return True
    warnings = person_dict.get("warnings")
    if isinstance(warnings, list) and "LOGIN_CONFLICT" in warnings:
        return True
    return False


def _is_person_excluded(p: dict[str, Any]) -> bool:
    return bool(p.get("exclude") is True or p.get("override_mode") == "exclude")


def _ach_reg_id(kind: str, raw_text: str) -> str:
    if kind == "patent" and (m := re.search(r"ZL\s?\d{8,13}(?:\.\d)?", raw_text, re.I)):
        return norm(m.group(0))
    if kind == "software" and (m := re.search(r"\d{4}SR\d{5,8}", raw_text, re.I)):
        return norm(m.group(0))
    return ""


def _match_existing_person(
    person_dict: dict[str, Any],
    pname: str,
    fp_val: str,
    fields: dict[str, object],
    existing_people: Sequence[LabPerson],
    incoming_name_counts: dict[str, int],
) -> LabPerson | None:
    if _has_login_conflict(person_dict):
        return None
    by_fp = {p.entity_fingerprint: p for p in existing_people}
    if fp_val in by_fp:
        return by_fp[fp_val]

    raw_match = person_dict.get("match")
    if isinstance(raw_match, dict) and raw_match.get("user_id"):
        try:
            uid = uuid.UUID(str(raw_match["user_id"]))
            for p in existing_people:
                if p.user_id == uid:
                    return p
        except ValueError:
            pass

    p_email = fields.get("email")
    if isinstance(p_email, str) and p_email:
        for p in existing_people:
            if p.email and p.email.strip().lower() == p_email.lower():
                return p

    norm_name = norm(pname)
    norm_aff = norm(str(fields.get("affiliation") or ""))
    if norm_name:
        inc_email = p_email if isinstance(p_email, str) and p_email else None
        inc_non_t1_fp = person_fp(
            tier="T3" if inc_email else "T2",
            login_name=None,
            name=pname,
            affiliation=str(fields.get("affiliation") or "") or None,
            email=inc_email,
        )
        inc_has_t1 = (str(person_dict.get("tier") or "") == "T1") or (
            fp_val != inc_non_t1_fp
        )

        def _can_fallback_to_person(p: LabPerson) -> bool:
            p_non_t1_fp = person_fp(
                tier="T3" if p.email else "T2",
                login_name=None,
                name=p.name,
                affiliation=p.affiliation,
                email=p.email,
            )
            ex_has_t1 = p.entity_fingerprint != p_non_t1_fp
            if inc_has_t1 and (ex_has_t1 or p.user_id is not None):
                return False
            if (
                inc_email
                and p.email
                and p.email.strip().lower() != inc_email.lower()
            ):
                return False
            return True

        same_name_aff = [
            p
            for p in existing_people
            if norm(p.name) == norm_name
            and norm(p.affiliation or "") == norm_aff
            and _can_fallback_to_person(p)
        ]
        if len(same_name_aff) == 1 and incoming_name_counts.get(norm_name, 0) == 1:
            return same_name_aff[0]

        same_name = [
            p
            for p in existing_people
            if norm(p.name) == norm_name and _can_fallback_to_person(p)
        ]
        if len(same_name) == 1 and incoming_name_counts.get(norm_name, 0) == 1:
            return same_name[0]

    return None


def _match_existing_project(
    fp_val: str,
    fields: dict[str, object],
    existing_projects: Sequence[LabProject],
) -> LabProject | None:
    for p in existing_projects:
        if p.entity_fingerprint == fp_val:
            return p
    grant_no = norm(str(fields.get("grant_no") or ""))
    if grant_no:
        same_grant = [
            p
            for p in existing_projects
            if p.grant_no and norm(p.grant_no) == grant_no
        ]
        if len(same_grant) == 1:
            return same_grant[0]
    ntitle = norm(str(fields.get("title") or ""))[:120]
    nfunder = norm(str(fields.get("funder") or ""))
    if ntitle:
        same_title_funder = [
            p
            for p in existing_projects
            if norm(p.title)[:120] == ntitle
            and norm(p.funder or "") == nfunder
            and not (
                grant_no
                and norm(p.grant_no or "")
                and grant_no != norm(p.grant_no or "")
            )
        ]
        if len(same_title_funder) == 1:
            return same_title_funder[0]
        same_title = [
            p
            for p in existing_projects
            if norm(p.title)[:120] == ntitle
            and not (
                grant_no
                and norm(p.grant_no or "")
                and grant_no != norm(p.grant_no or "")
            )
            and not (
                nfunder
                and norm(p.funder or "")
                and nfunder != norm(p.funder or "")
            )
        ]
        if len(same_title) == 1:
            return same_title[0]
    return None


def _match_existing_achievement(
    fp_val: str,
    fields: dict[str, object],
    existing_achievements: Sequence[LabAchievement],
) -> LabAchievement | None:
    for a in existing_achievements:
        if a.entity_fingerprint == fp_val:
            return a
    ntitle = norm(str(fields.get("title") or ""))[:120]
    year_val = fields.get("year")
    inc_kind_raw = str(fields.get("kind") or "other")
    inc_kind = norm(inc_kind_raw)
    inc_reg = _ach_reg_id(inc_kind_raw, str(fields.get("raw_text") or ""))
    if ntitle:
        candidates: list[LabAchievement] = []
        for a in existing_achievements:
            if norm(str(a.kind or "other")) != inc_kind:
                continue
            ex_reg = _ach_reg_id(str(a.kind or "other"), str(a.raw_text or ""))
            if inc_reg and ex_reg and inc_reg != ex_reg:
                continue
            if norm(a.title)[:120] == ntitle and a.year == year_val:
                candidates.append(a)
        if len(candidates) == 1:
            return candidates[0]
    return None


def _match_existing_facility(
    fp_val: str,
    fields: dict[str, object],
    existing_facilities: Sequence[LabFacility],
) -> LabFacility | None:
    for f in existing_facilities:
        if f.entity_fingerprint == fp_val:
            return f
    ncat = norm(str(fields.get("category") or ""))
    nname = norm(str(fields.get("name") or ""))[:120]
    nspec = norm(str(fields.get("spec") or ""))[:80]
    if nname:
        candidates: list[LabFacility] = []
        for f in existing_facilities:
            ex_spec = norm(f.spec or "")[:80]
            if nspec and ex_spec and nspec != ex_spec:
                continue
            if norm(f.category) == ncat and norm(f.name)[:120] == nname:
                candidates.append(f)
        if len(candidates) == 1:
            return candidates[0]
    return None


async def ensure_baseline_provenance(session: AsyncSession) -> int:
    """Ensure every existing entity field with a non-blank value has at least one provenance row."""
    all_people = list(await session.scalars(select(LabPerson)))
    all_projects = list(await session.scalars(select(LabProject)))
    all_achievements = list(await session.scalars(select(LabAchievement)))
    all_facilities = list(await session.scalars(select(LabFacility)))

    live_rows = list(
        await session.scalars(
            select(FieldProvenance).where(FieldProvenance.superseded.is_(False))
        )
    )
    covered: set[tuple[str, uuid.UUID, str]] = {
        (r.entity_type, r.entity_id, r.field) for r in live_rows
    }

    created = 0
    entity_groups: list[tuple[EntityType, Sequence[Any]]] = [
        ("person", all_people),
        ("project", all_projects),
        ("achievement", all_achievements),
        ("facility", all_facilities),
    ]
    for entity_type, group in entity_groups:
        for ent in group:
            src_job_id = getattr(ent, "source_import_id", None)
            as_of = getattr(ent, "as_of_time", None) or date.today()
            for field in MANAGED_FIELDS[entity_type]:
                if (entity_type, ent.id, field) in covered:
                    continue
                val = get_entity_json_value(entity_type, ent, field)
                if _is_blank(val):
                    continue
                session.add(
                    FieldProvenance(
                        entity_type=entity_type,
                        entity_id=ent.id,
                        field=field,
                        value=val,
                        source_kind="import" if src_job_id is not None else "legacy",
                        source_job_id=src_job_id,
                        as_of_time=as_of,
                        recorded_by=None,
                        superseded=False,
                    )
                )
                covered.add((entity_type, ent.id, field))
                created += 1

    if created > 0:
        await session.flush()
    return created


async def backfill_field_provenance(session: AsyncSession) -> dict[str, object]:
    """Idempotently backfill `field_provenance` for existing jobs and entities (Spec §10.7)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    manual_entity_ids = set(
        await session.scalars(
            select(FieldProvenance.entity_id).where(
                FieldProvenance.source_kind == "manual"
            )
        )
    )
    del_stmt = delete(FieldProvenance).where(
        FieldProvenance.source_kind.in_(("import", "legacy")),
        FieldProvenance.recorded_by.is_(None),
        FieldProvenance.superseded.is_(False),
    )
    if manual_entity_ids:
        del_stmt = del_stmt.where(FieldProvenance.entity_id.notin_(manual_entity_ids))
    await session.execute(del_stmt)
    await session.flush()

    jobs = list(
        await session.scalars(
            select(ImportJob)
            .where(ImportJob.status.in_(("provisioned", "merged")))
            .order_by(
                ImportJob.as_of_time.asc().nulls_first(),
                ImportJob.created_at.asc(),
            )
        )
    )
    existing_users = list(await session.scalars(select(User)))
    all_people = list(await session.scalars(select(LabPerson)))
    all_projects = list(await session.scalars(select(LabProject)))
    all_achievements = list(await session.scalars(select(LabAchievement)))
    all_facilities = list(await session.scalars(select(LabFacility)))

    all_prov_rows = list(await session.scalars(select(FieldProvenance)))
    all_rows_by_ef: dict[tuple[str, uuid.UUID, str], list[FieldProvenance]] = {}
    by_ef: dict[tuple[str, uuid.UUID, str], list[FieldProvenance]] = {}
    for r in all_prov_rows:
        all_rows_by_ef.setdefault((r.entity_type, r.entity_id, r.field), []).append(r)
        if not r.superseded:
            by_ef.setdefault((r.entity_type, r.entity_id, r.field), []).append(r)

    created_count = 0

    def _add_prov_if_missing(
        entity_type: EntityType,
        entity_id: uuid.UUID,
        field: str,
        val: object,
        source_kind: str,
        source_job_id: uuid.UUID | None,
        as_of_date: date,
    ) -> None:
        nonlocal created_count
        if _is_blank(val):
            return
        all_list = all_rows_by_ef.setdefault((entity_type, entity_id, field), [])
        cur_list = by_ef.setdefault((entity_type, entity_id, field), [])
        for existing in all_list:
            if existing.source_kind == source_kind and existing.source_job_id == source_job_id:
                if source_job_id is not None or existing.value == val:
                    return
        new_row = FieldProvenance(
            entity_type=entity_type,
            entity_id=entity_id,
            field=field,
            value=val,
            source_kind=source_kind,
            source_job_id=source_job_id,
            as_of_time=as_of_date,
            recorded_by=None,
            superseded=False,
        )
        session.add(new_row)
        all_list.append(new_row)
        cur_list.append(new_row)
        created_count += 1

    # 1. Replay all provisioned / merged ImportJobs in ascending as_of_time order
    for job in jobs:
        preview: dict[str, Any] = dict(job.preview or {})
        job_as_of = (
            job.as_of_time
            or _parse_iso_date(preview.get("as_of_time"))
            or (job.created_at.date() if job.created_at else date.today())
        )
        raw_ppl = preview.get("people")
        ppl_list: list[dict[str, Any]] = [
            dict(p)
            for p in (raw_ppl if isinstance(raw_ppl, list) else [])
            if isinstance(p, dict) and not _is_person_excluded(p)
        ]
        if ppl_list:
            for p in ppl_list:
                if p.get("tier") == "T1" and not p.get("cert"):
                    p["cert"] = p.get("login_name")
            assign_people_identities(
                ppl_list,
                existing_users=existing_users,
                existing_people=all_people,
            )
        inc_name_counts: dict[str, int] = {}
        for p in ppl_list:
            nm = norm(str(p.get("name") or ""))
            if nm:
                inc_name_counts[nm] = inc_name_counts.get(nm, 0) + 1

        for p in ppl_list:
            pname, fp_val, p_fields = _extract_person_incoming(p)
            matched_p = _match_existing_person(
                p, pname, fp_val, p_fields, all_people, inc_name_counts
            )
            if matched_p is not None:
                for field, val in p_fields.items():
                    _add_prov_if_missing(
                        "person", matched_p.id, field, val, "import", job.id, job_as_of
                    )

        raw_projs = preview.get("projects")
        for proj in (raw_projs if isinstance(raw_projs, list) else []):
            if not isinstance(proj, dict):
                continue
            _, fp_val, j_fields = _extract_project_incoming(proj, job_as_of)
            matched_j = _match_existing_project(fp_val, j_fields, all_projects)
            if matched_j is not None:
                for field, val in j_fields.items():
                    _add_prov_if_missing(
                        "project", matched_j.id, field, val, "import", job.id, job_as_of
                    )

        raw_achs = preview.get("achievements")
        for ach in (raw_achs if isinstance(raw_achs, list) else []):
            if not isinstance(ach, dict):
                continue
            _, fp_val, a_fields = _extract_achievement_incoming(ach)
            matched_a = _match_existing_achievement(fp_val, a_fields, all_achievements)
            if matched_a is not None:
                for field, val in a_fields.items():
                    _add_prov_if_missing(
                        "achievement",
                        matched_a.id,
                        field,
                        val,
                        "import",
                        job.id,
                        job_as_of,
                    )

        raw_facs = preview.get("facilities")
        for fac in (raw_facs if isinstance(raw_facs, list) else []):
            if not isinstance(fac, dict):
                continue
            _, fp_val, f_fields = _extract_facility_incoming(fac)
            matched_f = _match_existing_facility(fp_val, f_fields, all_facilities)
            if matched_f is not None:
                for field, val in f_fields.items():
                    _add_prov_if_missing(
                        "facility",
                        matched_f.id,
                        field,
                        val,
                        "import",
                        job.id,
                        job_as_of,
                    )

    # 2. For any entity field still lacking a provenance entry matching its current value
    jobs_by_id = {j.id: j for j in jobs}
    entity_groups: list[tuple[EntityType, Sequence[Any]]] = [
        ("person", all_people),
        ("project", all_projects),
        ("achievement", all_achievements),
        ("facility", all_facilities),
    ]
    for entity_type, group in entity_groups:
        for ent in group:
            src_job_id = getattr(ent, "source_import_id", None)
            ent_as_of = getattr(ent, "as_of_time", None) or date.today()
            for field in MANAGED_FIELDS[entity_type]:
                cur_val = get_entity_json_value(entity_type, ent, field)
                if _is_blank(cur_val):
                    continue
                f_rows = by_ef.get((entity_type, ent.id, field), [])
                if not f_rows:
                    if src_job_id is not None and src_job_id in jobs_by_id:
                        _add_prov_if_missing(
                            entity_type,
                            ent.id,
                            field,
                            cur_val,
                            "import",
                            src_job_id,
                            ent_as_of,
                        )
                    else:
                        _add_prov_if_missing(
                            entity_type,
                            ent.id,
                            field,
                            cur_val,
                            "legacy",
                            None,
                            ent_as_of,
                        )
                elif not any(r.value == cur_val for r in f_rows):
                    upd_at = getattr(ent, "updated_at", None)
                    leg_date = upd_at.date() if isinstance(upd_at, datetime) else ent_as_of
                    _add_prov_if_missing(
                        entity_type,
                        ent.id,
                        field,
                        cur_val,
                        "legacy",
                        None,
                        leg_date,
                    )

    await session.flush()

    # 3. Compute discrepancies without modifying entity tables
    discrepancies: list[dict[str, object]] = []
    total_live = 0
    for entity_type, group in entity_groups:
        for ent in group:
            for field in MANAGED_FIELDS[entity_type]:
                f_rows = [
                    r
                    for r in by_ef.get((entity_type, ent.id, field), [])
                    if not r.superseded
                ]
                total_live += len(f_rows)
                cur_val = get_entity_json_value(entity_type, ent, field)
                eff = effective_value(f_rows)
                eff_val = eff.value if eff is not None else None
                if _is_blank(cur_val) and _is_blank(eff_val):
                    continue
                if cur_val != eff_val:
                    discrepancies.append(
                        {
                            "entity_type": entity_type,
                            "entity_id": str(ent.id),
                            "field": field,
                            "current_value": cur_val,
                            "effective_value": eff_val,
                        }
                    )

    return {
        "backfilled_entries": created_count if created_count > 0 else total_live,
        "created_entries": created_count,
        "total_entries": total_live,
        "discrepancies": discrepancies,
    }


def _resolve_job_as_of(
    job: ImportJob, as_of_time_override: date | None = None
) -> date:
    preview: dict[str, Any] = dict(job.preview or {})
    parsed_preview_date = _parse_iso_date(preview.get("as_of_time"))
    as_of_date = as_of_time_override or job.as_of_time or parsed_preview_date
    if as_of_date is None:
        raise ProvisionError(422, "as_of_time_required", "请提供本子基准时间 as_of_time")
    return as_of_date


def _consolidate_incoming_fields(
    entity_type: EntityType,
    entity: Any,
    acc_fields: dict[str, object],
    new_fields: dict[str, object],
) -> None:
    """Consolidate duplicate incoming entries matching the same existing entity within one job."""
    for field in MANAGED_FIELDS[entity_type]:
        inc_val = new_fields.get(field)
        if _is_blank(inc_val):
            continue
        prev_val = acc_fields.get(field)
        if entity_type == "person" and field == "project_role":
            if prev_val == "applicant" or inc_val == "applicant":
                acc_fields[field] = "applicant"
                continue
        if entity_type == "project" and field == "status":
            if prev_val not in (None, "", "unknown") and inc_val == "unknown":
                continue
            if prev_val in (None, "", "unknown") and inc_val != "unknown":
                acc_fields[field] = inc_val
                continue
        cur_val = get_entity_json_value(entity_type, entity, field)
        if _is_blank(prev_val):
            acc_fields[field] = inc_val
        elif prev_val == cur_val and inc_val != cur_val:
            acc_fields[field] = inc_val


async def _reconcile_job_existing_provenance(
    session: AsyncSession,
    job_id: uuid.UUID,
    new_as_of: date,
) -> None:
    """Reconcile existing FieldProvenance rows and affected entities when a job's as_of_time changes."""
    job_rows = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.source_job_id == job_id,
                FieldProvenance.superseded.is_(False),
            )
        )
    )
    model_map: dict[EntityType, Any] = {
        "person": LabPerson,
        "project": LabProject,
        "achievement": LabAchievement,
        "facility": LabFacility,
    }
    affected_entities: set[tuple[EntityType, uuid.UUID]] = set()
    has_date_change = any(r.as_of_time != new_as_of for r in job_rows)
    for r in job_rows:
        if r.as_of_time != new_as_of:
            r.as_of_time = new_as_of
        if r.entity_type in MANAGED_FIELDS:
            affected_entities.add((r.entity_type, r.entity_id))  # type: ignore[arg-type]

    for etype, model_cls in model_map.items():
        src_ents = list(
            await session.scalars(
                select(model_cls).where(model_cls.source_import_id == job_id)
            )
        )
        for ent in src_ents:
            if ent.as_of_time != new_as_of:
                has_date_change = True
                ent.as_of_time = new_as_of
            affected_entities.add((etype, ent.id))

    if not has_date_change:
        return

    await session.flush()

    for etype, eid in affected_entities:
        model_cls = model_map[etype]
        ent = await session.get(model_cls, eid)
        if ent is None:
            continue
        ent_rows = list(
            await session.scalars(
                select(FieldProvenance).where(
                    FieldProvenance.entity_type == etype,
                    FieldProvenance.entity_id == eid,
                    FieldProvenance.superseded.is_(False),
                )
            )
        )
        if not ent_rows:
            continue
        by_field: dict[str, list[FieldProvenance]] = {}
        for r in ent_rows:
            by_field.setdefault(r.field, []).append(r)

        prior_affiliation = getattr(ent, "affiliation", None)
        prior_email = getattr(ent, "email", None)
        field_changed = False
        for field in MANAGED_FIELDS[etype]:
            f_list = by_field.get(field)
            if not f_list:
                continue
            cur_val = get_entity_json_value(etype, ent, field)
            eff = effective_value(f_list)
            eff_val = eff.value if eff is not None else None
            if eff_val != cur_val:
                set_entity_field_from_json(etype, ent, field, eff_val)
                field_changed = True

        if field_changed:
            await _refresh_and_validate_entity_fingerprint(
                session,
                etype,
                ent,
                prior_affiliation=prior_affiliation,
                prior_email=prior_email,
            )

        non_manual_dates = [
            r.as_of_time
            for r in ent_rows
            if r.source_kind != "manual" and r.as_of_time is not None
        ]
        if non_manual_dates:
            ent.as_of_time = max(non_manual_dates)

    await session.flush()


async def build_merge_preview(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    as_of_time_override: date | None = None,
) -> dict[str, object]:
    """Compute Phase 2 temporal merge preview without mutating entity tables (Spec §10.5)."""
    job = await session.get(ImportJob, job_id)
    if job is None:
        raise ProvisionError(404, "not_found", "导入任务不存在")
    if job.status not in ("parsed", "provisioned", "merged"):
        raise ProvisionError(409, "import_invalid_status", "该导入任务当前状态不可合并")

    as_of_date = _resolve_job_as_of(job, as_of_time_override)
    await ensure_baseline_provenance(session)

    existing_users = list(await session.scalars(select(User)))
    existing_people = list(await session.scalars(select(LabPerson)))
    existing_projects = list(await session.scalars(select(LabProject)))
    existing_achievements = list(await session.scalars(select(LabAchievement)))
    existing_facilities = list(await session.scalars(select(LabFacility)))
    all_jobs = {j.id: j for j in await session.scalars(select(ImportJob))}

    live_rows = list(
        await session.scalars(
            select(FieldProvenance).where(FieldProvenance.superseded.is_(False))
        )
    )
    by_ef: dict[tuple[str, uuid.UUID, str], list[FieldProvenance]] = {}
    for r in live_rows:
        row_obj = r
        if (
            r.source_job_id == job.id
            and r.source_kind == "import"
            and r.as_of_time != as_of_date
        ):
            row_obj = FieldProvenance(
                id=r.id,
                entity_type=r.entity_type,
                entity_id=r.entity_id,
                field=r.field,
                value=r.value,
                source_kind=r.source_kind,
                source_job_id=r.source_job_id,
                as_of_time=as_of_date,
                recorded_by=r.recorded_by,
                recorded_at=r.recorded_at,
                superseded=False,
            )
        by_ef.setdefault((r.entity_type, r.entity_id, r.field), []).append(row_obj)

    preview: dict[str, Any] = dict(job.preview or {})

    raw_ppl = preview.get("people")
    ppl_list: list[dict[str, Any]] = [
        dict(p)
        for p in (raw_ppl if isinstance(raw_ppl, list) else [])
        if isinstance(p, dict) and not _is_person_excluded(p)
    ]
    if ppl_list:
        for p in ppl_list:
            if p.get("tier") == "T1" and not p.get("cert"):
                p["cert"] = p.get("login_name")
        assign_people_identities(
            ppl_list,
            existing_users=existing_users,
            existing_people=existing_people,
        )

    inc_name_counts: dict[str, int] = {}
    for p in ppl_list:
        nm = norm(str(p.get("name") or ""))
        if nm:
            inc_name_counts[nm] = inc_name_counts.get(nm, 0) + 1

    summary: dict[str, int] = {
        "added": 0,
        "updated": 0,
        "fill_blank": 0,
        "time_regression": 0,
        "manual_protected": 0,
        "same_time_conflict": 0,
    }
    changes: list[dict[str, object]] = []
    added_entities: list[dict[str, object]] = []

    def _evaluate_entity_fields(
        entity_type: EntityType,
        entity: Any,
        entity_name: str,
        incoming_fields: dict[str, object],
    ) -> None:
        for field in MANAGED_FIELDS[entity_type]:
            inc_val = incoming_fields.get(field)
            f_rows = by_ef.get((entity_type, entity.id, field), [])
            eff = effective_value(f_rows)
            if any(r.source_job_id == job.id for r in f_rows):
                cur_val = eff.value if eff is not None else None
            else:
                cur_val = get_entity_json_value(entity_type, entity, field)
            if _is_blank(inc_val) or inc_val == cur_val:
                continue
            cur_as_of = (
                eff.as_of_time
                if eff is not None and eff.as_of_time is not None
                else getattr(entity, "as_of_time", None)
            )
            cur_kind = (
                eff.source_kind
                if eff is not None
                else (
                    "import"
                    if getattr(entity, "source_import_id", None) is not None
                    else "legacy"
                )
            )
            cur_job_id = (
                eff.source_job_id
                if eff is not None
                else getattr(entity, "source_import_id", None)
            )
            cur_job_filename = (
                all_jobs[cur_job_id].original_filename
                if cur_job_id is not None and cur_job_id in all_jobs
                else None
            )

            category: MergeCategory
            default_action: Literal["adopt_incoming", "keep_current"]
            requires_decision = False

            if eff is not None and eff.source_kind == "manual":
                category = "manual_protected"
                default_action = "keep_current"
            elif _is_blank(cur_val):
                category = "fill_blank"
                default_action = "adopt_incoming"
            elif cur_as_of is None or as_of_date > cur_as_of:
                category = "updated"
                default_action = "adopt_incoming"
            elif as_of_date < cur_as_of:
                category = "time_regression"
                default_action = "keep_current"
            else:
                if cur_kind == "import" and cur_job_id != job.id:
                    category = "same_time_conflict"
                    default_action = "keep_current"
                    requires_decision = True
                else:
                    category = "updated"
                    default_action = "adopt_incoming"

            summary[category] += 1
            cur_as_of_str = cur_as_of.isoformat() if cur_as_of is not None else None
            inc_as_of_str = as_of_date.isoformat()
            changes.append(
                {
                    "change_key": f"{entity_type}:{entity.id}:{field}",
                    "fingerprint_key": f"{entity_type}:{entity.entity_fingerprint}:{field}",
                    "category": category,
                    "entity_type": entity_type,
                    "entity_id": str(entity.id),
                    "entity_name": entity_name,
                    "field": field,
                    "current_value": cur_val,
                    "current_as_of_time": cur_as_of_str,
                    "current_source_kind": cur_kind,
                    "current_source_job_id": (
                        str(cur_job_id) if cur_job_id is not None else None
                    ),
                    "current_source_filename": cur_job_filename,
                    "incoming_value": inc_val,
                    "incoming_as_of_time": inc_as_of_str,
                    "incoming_source_filename": job.original_filename,
                    "comparison_text": (
                        f"{inc_val} ({inc_as_of_str}) → 当前：{cur_val} ({cur_as_of_str or '未知'})"
                    ),
                    "default_action": default_action,
                    "requires_decision": requires_decision,
                }
            )

    # People
    seen_person_fps = {p.entity_fingerprint for p in existing_people}
    matched_people_map: dict[uuid.UUID, tuple[LabPerson, dict[str, object]]] = {}
    for p in ppl_list:
        pname, fp_val, p_fields = _extract_person_incoming(p)
        if not pname:
            continue
        matched_p = _match_existing_person(
            p, pname, fp_val, p_fields, existing_people, inc_name_counts
        )
        if matched_p is None:
            if fp_val in seen_person_fps:
                continue
            seen_person_fps.add(fp_val)
            summary["added"] += 1
            added_entities.append(
                {
                    "entity_type": "person",
                    "name": pname,
                    "fingerprint": fp_val,
                    "fields": p_fields,
                    "raw": p,
                }
            )
        else:
            if matched_p.id not in matched_people_map:
                acc_p: dict[str, object] = {f: None for f in PERSON_MANAGED_FIELDS}
                _consolidate_incoming_fields("person", matched_p, acc_p, p_fields)
                matched_people_map[matched_p.id] = (matched_p, acc_p)
            else:
                _consolidate_incoming_fields(
                    "person", matched_p, matched_people_map[matched_p.id][1], p_fields
                )
    for matched_p, acc_p in matched_people_map.values():
        _evaluate_entity_fields("person", matched_p, matched_p.name, acc_p)

    # Projects
    seen_proj_fps = {p.entity_fingerprint for p in existing_projects}
    matched_proj_map: dict[uuid.UUID, tuple[LabProject, dict[str, object]]] = {}
    raw_projs = preview.get("projects")
    for proj in (raw_projs if isinstance(raw_projs, list) else []):
        if not isinstance(proj, dict):
            continue
        title, fp_val, j_fields = _extract_project_incoming(proj, as_of_date)
        if not title:
            continue
        matched_j = _match_existing_project(fp_val, j_fields, existing_projects)
        if matched_j is None:
            if fp_val in seen_proj_fps:
                continue
            seen_proj_fps.add(fp_val)
            summary["added"] += 1
            added_entities.append(
                {
                    "entity_type": "project",
                    "name": title,
                    "fingerprint": fp_val,
                    "fields": j_fields,
                }
            )
        else:
            if matched_j.id not in matched_proj_map:
                acc_j: dict[str, object] = {f: None for f in PROJECT_MANAGED_FIELDS}
                _consolidate_incoming_fields("project", matched_j, acc_j, j_fields)
                matched_proj_map[matched_j.id] = (matched_j, acc_j)
            else:
                _consolidate_incoming_fields(
                    "project", matched_j, matched_proj_map[matched_j.id][1], j_fields
                )
    for matched_j, acc_j in matched_proj_map.values():
        _evaluate_entity_fields("project", matched_j, matched_j.title, acc_j)

    # Achievements
    seen_ach_fps = {a.entity_fingerprint for a in existing_achievements}
    matched_ach_map: dict[uuid.UUID, tuple[LabAchievement, dict[str, object]]] = {}
    raw_achs = preview.get("achievements")
    for ach in (raw_achs if isinstance(raw_achs, list) else []):
        if not isinstance(ach, dict):
            continue
        title, fp_val, a_fields = _extract_achievement_incoming(ach)
        if not title:
            continue
        matched_a = _match_existing_achievement(fp_val, a_fields, existing_achievements)
        if matched_a is None:
            if fp_val in seen_ach_fps:
                continue
            seen_ach_fps.add(fp_val)
            summary["added"] += 1
            added_entities.append(
                {
                    "entity_type": "achievement",
                    "name": title,
                    "fingerprint": fp_val,
                    "fields": a_fields,
                }
            )
        else:
            if matched_a.id not in matched_ach_map:
                acc_a: dict[str, object] = {f: None for f in ACHIEVEMENT_MANAGED_FIELDS}
                _consolidate_incoming_fields("achievement", matched_a, acc_a, a_fields)
                matched_ach_map[matched_a.id] = (matched_a, acc_a)
            else:
                _consolidate_incoming_fields(
                    "achievement", matched_a, matched_ach_map[matched_a.id][1], a_fields
                )
    for matched_a, acc_a in matched_ach_map.values():
        _evaluate_entity_fields("achievement", matched_a, matched_a.title, acc_a)

    # Facilities
    seen_fac_fps = {f.entity_fingerprint for f in existing_facilities}
    matched_fac_map: dict[uuid.UUID, tuple[LabFacility, dict[str, object]]] = {}
    raw_facs = preview.get("facilities")
    for fac in (raw_facs if isinstance(raw_facs, list) else []):
        if not isinstance(fac, dict):
            continue
        name, fp_val, f_fields = _extract_facility_incoming(fac)
        if not name:
            continue
        matched_f = _match_existing_facility(fp_val, f_fields, existing_facilities)
        if matched_f is None:
            if fp_val in seen_fac_fps:
                continue
            seen_fac_fps.add(fp_val)
            summary["added"] += 1
            added_entities.append(
                {
                    "entity_type": "facility",
                    "name": name,
                    "fingerprint": fp_val,
                    "fields": f_fields,
                }
            )
        else:
            if matched_f.id not in matched_fac_map:
                acc_f: dict[str, object] = {f: None for f in FACILITY_MANAGED_FIELDS}
                _consolidate_incoming_fields("facility", matched_f, acc_f, f_fields)
                matched_fac_map[matched_f.id] = (matched_f, acc_f)
            else:
                _consolidate_incoming_fields(
                    "facility", matched_f, matched_fac_map[matched_f.id][1], f_fields
                )
    for matched_f, acc_f in matched_fac_map.values():
        _evaluate_entity_fields("facility", matched_f, matched_f.name, acc_f)

    return {
        "job_id": str(job.id),
        "as_of_time": as_of_date.isoformat(),
        "summary": summary,
        "changes": changes,
        "added_entities": [
            {
                "entity_type": item["entity_type"],
                "name": item["name"],
                "fingerprint": item["fingerprint"],
            }
            for item in added_entities
        ],
    }


def _lookup_decision(
    decisions_map: dict[str, MergeAction],
    change_item: dict[str, object],
) -> MergeAction | None:
    ck = str(change_item.get("change_key") or "")
    if ck in decisions_map:
        return decisions_map[ck]
    fk = str(change_item.get("fingerprint_key") or "")
    if fk and fk in decisions_map:
        return decisions_map[fk]
    etype = str(change_item.get("entity_type") or "")
    ename = str(change_item.get("entity_name") or "")
    field = str(change_item.get("field") or "")
    nk = f"{etype}:{ename}:{field}"
    if nk in decisions_map:
        return decisions_map[nk]
    return None


async def execute_merge(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    actor_id: uuid.UUID,
    body: MergeRequest,
    embedder: Embedder,
) -> dict[str, object]:
    """Execute Phase 2 temporal merge with admin decisions and RAG re-indexing (Spec §10.5)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    job = await session.scalar(
        select(ImportJob).where(ImportJob.id == job_id).with_for_update()
    )
    if job is None:
        raise ProvisionError(404, "not_found", "导入任务不存在")
    if job.status not in ("parsed", "provisioned", "merged"):
        raise ProvisionError(409, "import_invalid_status", "该导入任务当前状态不可合并")

    as_of_date = _resolve_job_as_of(job, body.as_of_time)
    await _reconcile_job_existing_provenance(session, job.id, as_of_date)
    preview_result = await build_merge_preview(
        session,
        job_id=job_id,
        as_of_time_override=as_of_date,
    )
    raw_changes = preview_result.get("changes")
    changes_list: list[dict[str, object]] = (
        list(raw_changes) if isinstance(raw_changes, list) else []
    )

    decisions_map: dict[str, MergeAction] = {
        d.change_key: d.action for d in body.decisions
    }

    # Verify all same_time_conflict items have an explicit decision
    for ch in changes_list:
        if ch.get("category") == "same_time_conflict":
            chosen = _lookup_decision(decisions_map, ch)
            if chosen not in ("adopt_incoming", "keep_current", "force_adopt"):
                raise ProvisionError(
                    422,
                    "merge_conflict_unresolved",
                    f"同时间冲突字段尚未裁决: {ch.get('change_key')}",
                )

    change_by_ef: dict[tuple[str, uuid.UUID, str], dict[str, object]] = {}
    for ch in changes_list:
        etype = str(ch.get("entity_type") or "")
        eid = uuid.UUID(str(ch["entity_id"]))
        fld = str(ch.get("field") or "")
        change_by_ef[(etype, eid, fld)] = ch

    existing_users = list(await session.scalars(select(User)))
    existing_people = list(
        await session.scalars(select(LabPerson).with_for_update())
    )
    existing_projects = list(
        await session.scalars(select(LabProject).with_for_update())
    )
    existing_achievements = list(
        await session.scalars(select(LabAchievement).with_for_update())
    )
    existing_facilities = list(
        await session.scalars(select(LabFacility).with_for_update())
    )

    used_person_fps = {p.entity_fingerprint for p in existing_people}
    used_person_uids = {p.user_id for p in existing_people if p.user_id is not None}
    used_proj_fps = {p.entity_fingerprint for p in existing_projects}
    used_ach_fps = {a.entity_fingerprint for a in existing_achievements}
    used_fac_fps = {f.entity_fingerprint for f in existing_facilities}

    live_rows = list(
        await session.scalars(
            select(FieldProvenance)
            .where(FieldProvenance.superseded.is_(False))
            .with_for_update()
        )
    )
    by_ef: dict[tuple[str, uuid.UUID, str], list[FieldProvenance]] = {}
    for r in live_rows:
        by_ef.setdefault((r.entity_type, r.entity_id, r.field), []).append(r)

    preview: dict[str, Any] = dict(job.preview or {})
    raw_ppl = preview.get("people")
    ppl_list: list[dict[str, Any]] = [
        dict(p)
        for p in (raw_ppl if isinstance(raw_ppl, list) else [])
        if isinstance(p, dict) and not _is_person_excluded(p)
    ]
    if ppl_list:
        for p in ppl_list:
            if p.get("tier") == "T1" and not p.get("cert"):
                p["cert"] = p.get("login_name")
        assign_people_identities(
            ppl_list,
            existing_users=existing_users,
            existing_people=existing_people,
        )

    inc_name_counts: dict[str, int] = {}
    for p in ppl_list:
        nm = norm(str(p.get("name") or ""))
        if nm:
            inc_name_counts[nm] = inc_name_counts.get(nm, 0) + 1

    added_count = 0
    updated_fields_count = 0
    kept_fields_count = 0
    forced_fields_count = 0

    async def _merge_existing_entity(
        entity_type: EntityType,
        entity: Any,
        incoming_fields: dict[str, object],
    ) -> None:
        nonlocal updated_fields_count, kept_fields_count, forced_fields_count
        prior_affiliation = getattr(entity, "affiliation", None)
        prior_email = getattr(entity, "email", None)
        for field in MANAGED_FIELDS[entity_type]:
            inc_val = incoming_fields.get(field)
            if _is_blank(inc_val):
                continue

            cur_val = get_entity_json_value(entity_type, entity, field)
            f_rows = by_ef.setdefault((entity_type, entity.id, field), [])
            ch = change_by_ef.get((entity_type, entity.id, field))

            for old_r in f_rows:
                if (
                    old_r.source_job_id == job.id
                    and old_r.source_kind == "import"
                    and not old_r.superseded
                ):
                    old_r.superseded = True

            imp_row = FieldProvenance(
                entity_type=entity_type,
                entity_id=entity.id,
                field=field,
                value=inc_val,
                source_kind="import",
                source_job_id=job.id,
                as_of_time=as_of_date,
                recorded_by=actor_id,
                superseded=False,
            )
            session.add(imp_row)
            f_rows.append(imp_row)

            if ch is not None:
                cat = str(ch.get("category") or "")
                default_act: MergeAction = (
                    "adopt_incoming"
                    if ch.get("default_action") == "adopt_incoming"
                    else "keep_current"
                )
                chosen_act = _lookup_decision(decisions_map, ch) or default_act

                if cat in ("time_regression", "manual_protected", "same_time_conflict"):
                    if chosen_act == "force_adopt":
                        man_row = FieldProvenance(
                            entity_type=entity_type,
                            entity_id=entity.id,
                            field=field,
                            value=inc_val,
                            source_kind="manual",
                            source_job_id=job.id,
                            as_of_time=as_of_date,
                            recorded_by=actor_id,
                            superseded=False,
                        )
                        session.add(man_row)
                        f_rows.append(man_row)
                        forced_fields_count += 1
                    elif cat == "same_time_conflict" and chosen_act == "keep_current":
                        imp_row.superseded = True
                elif cat in ("updated", "fill_blank") and chosen_act == "keep_current":
                    imp_row.superseded = True

            await session.flush()
            eff = effective_value(f_rows)
            eff_val = eff.value if eff is not None else None
            if eff_val != cur_val:
                set_entity_field_from_json(entity_type, entity, field, eff_val)
                updated_fields_count += 1
            elif ch is not None:
                kept_fields_count += 1

        old_fp = entity.entity_fingerprint
        await _refresh_and_validate_entity_fingerprint(
            session,
            entity_type,
            entity,
            prior_affiliation=prior_affiliation,
            prior_email=prior_email,
        )
        if entity.entity_fingerprint != old_fp:
            fp_set_map = {
                "person": used_person_fps,
                "project": used_proj_fps,
                "achievement": used_ach_fps,
                "facility": used_fac_fps,
            }
            target_set = fp_set_map[entity_type]
            target_set.discard(old_fp)
            target_set.add(entity.entity_fingerprint)

        if getattr(entity, "as_of_time", None) is None or as_of_date > entity.as_of_time:
            entity.as_of_time = as_of_date

    # 1. People (NEVER create or mutate User accounts during Phase 2 merge!)
    matched_people_map: dict[uuid.UUID, tuple[LabPerson, dict[str, object]]] = {}
    for p in ppl_list:
        pname, fp_val, p_fields = _extract_person_incoming(p)
        if not pname:
            continue
        if _has_login_conflict(p):
            raise ProvisionError(
                409,
                "login_conflict",
                f"成员 {pname} 存在登录名冲突，请先在预览中解决冲突后再合并",
            )
        matched_p = _match_existing_person(
            p, pname, fp_val, p_fields, existing_people, inc_name_counts
        )
        if matched_p is None:
            if fp_val in used_person_fps:
                continue
            linked_uid: uuid.UUID | None = None
            raw_match = p.get("match")
            if isinstance(raw_match, dict) and raw_match.get("status") == "existing" and raw_match.get("user_id"):
                try:
                    cand_uid = uuid.UUID(str(raw_match["user_id"]))
                    if cand_uid not in used_person_uids:
                        linked_uid = cand_uid
                except ValueError:
                    linked_uid = None

            used_person_fps.add(fp_val)
            if linked_uid is not None:
                used_person_uids.add(linked_uid)
            new_person = LabPerson(
                user_id=linked_uid,
                name=pname,
                email=p_fields["email"] if isinstance(p_fields["email"], str) else None,
                title=p_fields["title"] if isinstance(p_fields["title"], str) else None,
                admin_duty=(
                    p_fields["admin_duty"]
                    if isinstance(p_fields["admin_duty"], str)
                    else None
                ),
                tutor_role=(
                    p_fields["tutor_role"]
                    if isinstance(p_fields["tutor_role"], str)
                    else None
                ),
                research_specialty=(
                    p_fields["research_specialty"]
                    if isinstance(p_fields["research_specialty"], str)
                    else None
                ),
                degree=(
                    p_fields["degree"] if isinstance(p_fields["degree"], str) else None
                ),
                affiliation=(
                    p_fields["affiliation"]
                    if isinstance(p_fields["affiliation"], str)
                    else None
                ),
                project_role=str(p_fields["project_role"] or "member"),
                bio=p_fields["bio"] if isinstance(p_fields["bio"], str) else None,
                entity_fingerprint=fp_val,
                as_of_time=as_of_date,
                source_import_id=job.id,
            )
            session.add(new_person)
            await session.flush()
            await record_entity_import_provenance(
                session,
                entity_type="person",
                entity=new_person,
                job_id=job.id,
                as_of_time=as_of_date,
                recorded_by=actor_id,
            )
            added_count += 1
        else:
            if matched_p.id not in matched_people_map:
                acc_p: dict[str, object] = {f: None for f in PERSON_MANAGED_FIELDS}
                _consolidate_incoming_fields("person", matched_p, acc_p, p_fields)
                matched_people_map[matched_p.id] = (matched_p, acc_p)
            else:
                _consolidate_incoming_fields(
                    "person", matched_p, matched_people_map[matched_p.id][1], p_fields
                )
    for matched_p, acc_p in matched_people_map.values():
        await _merge_existing_entity("person", matched_p, acc_p)

    # 2. Projects
    matched_proj_map: dict[uuid.UUID, tuple[LabProject, dict[str, object]]] = {}
    raw_projs = preview.get("projects")
    for proj in (raw_projs if isinstance(raw_projs, list) else []):
        if not isinstance(proj, dict):
            continue
        title, fp_val, j_fields = _extract_project_incoming(proj, as_of_date)
        if not title:
            continue
        matched_j = _match_existing_project(fp_val, j_fields, existing_projects)
        if matched_j is None:
            if fp_val in used_proj_fps:
                continue
            used_proj_fps.add(fp_val)
            raw_lab_role = str(proj.get("lab_role") or "").strip().lower()
            default_lab_role = (
                raw_lab_role
                if raw_lab_role in ("host", "participant", "unknown")
                else "host"
            )
            new_proj = LabProject(
                title=title,
                funder=(
                    j_fields["funder"] if isinstance(j_fields["funder"], str) else None
                ),
                grant_no=(
                    j_fields["grant_no"]
                    if isinstance(j_fields["grant_no"], str)
                    else None
                ),
                pi_name=(
                    j_fields["pi_name"]
                    if isinstance(j_fields["pi_name"], str)
                    else None
                ),
                lab_role=str(j_fields["lab_role"] or default_lab_role),
                start_date=_parse_iso_date(j_fields["start_date"]),
                end_date=_parse_iso_date(j_fields["end_date"]),
                amount_wan=_parse_decimal(j_fields["amount_wan"]),
                status=str(j_fields["status"] or "unknown"),
                status_source=str(j_fields["status_source"] or "inferred"),
                relationship_note=(
                    j_fields["relationship_note"]
                    if isinstance(j_fields["relationship_note"], str)
                    else None
                ),
                raw_text=str(j_fields["raw_text"] or title),
                entity_fingerprint=fp_val,
                as_of_time=as_of_date,
                source_import_id=job.id,
            )
            session.add(new_proj)
            await session.flush()
            await record_entity_import_provenance(
                session,
                entity_type="project",
                entity=new_proj,
                job_id=job.id,
                as_of_time=as_of_date,
                recorded_by=actor_id,
            )
            added_count += 1
        else:
            if matched_j.id not in matched_proj_map:
                acc_j: dict[str, object] = {f: None for f in PROJECT_MANAGED_FIELDS}
                _consolidate_incoming_fields("project", matched_j, acc_j, j_fields)
                matched_proj_map[matched_j.id] = (matched_j, acc_j)
            else:
                _consolidate_incoming_fields(
                    "project", matched_j, matched_proj_map[matched_j.id][1], j_fields
                )
    for matched_j, acc_j in matched_proj_map.values():
        await _merge_existing_entity("project", matched_j, acc_j)

    # 3. Achievements
    matched_ach_map: dict[uuid.UUID, tuple[LabAchievement, dict[str, object]]] = {}
    raw_achs = preview.get("achievements")
    for ach in (raw_achs if isinstance(raw_achs, list) else []):
        if not isinstance(ach, dict):
            continue
        title, fp_val, a_fields = _extract_achievement_incoming(ach)
        if not title:
            continue
        matched_a = _match_existing_achievement(fp_val, a_fields, existing_achievements)
        if matched_a is None:
            if fp_val in used_ach_fps:
                continue
            used_ach_fps.add(fp_val)
            new_ach = LabAchievement(
                kind=str(a_fields["kind"] or "other"),
                title=title,
                first_author=(
                    a_fields["first_author"]
                    if isinstance(a_fields["first_author"], str)
                    else None
                ),
                authors_text=(
                    a_fields["authors_text"]
                    if isinstance(a_fields["authors_text"], str)
                    else None
                ),
                venue=(
                    a_fields["venue"] if isinstance(a_fields["venue"], str) else None
                ),
                year=a_fields["year"] if isinstance(a_fields["year"], int) else None,
                support_note=(
                    a_fields["support_note"]
                    if isinstance(a_fields["support_note"], str)
                    else None
                ),
                raw_text=str(a_fields["raw_text"] or title),
                parse_quality=str(a_fields["parse_quality"] or "structured"),
                entity_fingerprint=fp_val,
                as_of_time=as_of_date,
                source_import_id=job.id,
            )
            session.add(new_ach)
            await session.flush()
            await record_entity_import_provenance(
                session,
                entity_type="achievement",
                entity=new_ach,
                job_id=job.id,
                as_of_time=as_of_date,
                recorded_by=actor_id,
            )
            added_count += 1
        else:
            if matched_a.id not in matched_ach_map:
                acc_a: dict[str, object] = {f: None for f in ACHIEVEMENT_MANAGED_FIELDS}
                _consolidate_incoming_fields("achievement", matched_a, acc_a, a_fields)
                matched_ach_map[matched_a.id] = (matched_a, acc_a)
            else:
                _consolidate_incoming_fields(
                    "achievement", matched_a, matched_ach_map[matched_a.id][1], a_fields
                )
    for matched_a, acc_a in matched_ach_map.values():
        await _merge_existing_entity("achievement", matched_a, acc_a)

    # 4. Facilities
    matched_fac_map: dict[uuid.UUID, tuple[LabFacility, dict[str, object]]] = {}
    raw_facs = preview.get("facilities")
    for fac in (raw_facs if isinstance(raw_facs, list) else []):
        if not isinstance(fac, dict):
            continue
        name, fp_val, f_fields = _extract_facility_incoming(fac)
        if not name:
            continue
        matched_f = _match_existing_facility(fp_val, f_fields, existing_facilities)
        if matched_f is None:
            if fp_val in used_fac_fps:
                continue
            used_fac_fps.add(fp_val)
            new_fac = LabFacility(
                category=str(f_fields["category"] or "equipment"),
                name=name,
                spec=f_fields["spec"] if isinstance(f_fields["spec"], str) else None,
                value_wan=_parse_decimal(f_fields["value_wan"]),
                usage_scenario=(
                    f_fields["usage_scenario"]
                    if isinstance(f_fields["usage_scenario"], str)
                    else None
                ),
                raw_text=str(f_fields["raw_text"] or name),
                entity_fingerprint=fp_val,
                as_of_time=as_of_date,
                source_import_id=job.id,
            )
            session.add(new_fac)
            await session.flush()
            await record_entity_import_provenance(
                session,
                entity_type="facility",
                entity=new_fac,
                job_id=job.id,
                as_of_time=as_of_date,
                recorded_by=actor_id,
            )
            added_count += 1
        else:
            if matched_f.id not in matched_fac_map:
                acc_f: dict[str, object] = {f: None for f in FACILITY_MANAGED_FIELDS}
                _consolidate_incoming_fields("facility", matched_f, acc_f, f_fields)
                matched_fac_map[matched_f.id] = (matched_f, acc_f)
            else:
                _consolidate_incoming_fields(
                    "facility", matched_f, matched_fac_map[matched_f.id][1], f_fields
                )
    for matched_f, acc_f in matched_fac_map.values():
        await _merge_existing_entity("facility", matched_f, acc_f)

    cur_stats = dict(job.stats or {})
    if job.status in ("parsed", "provisioned"):
        cur_stats["pre_merge_status"] = job.status
    preview["as_of_time"] = as_of_date.isoformat()
    job.preview = preview
    job.as_of_time = as_of_date
    job.status = "merged"
    await session.flush()

    doc_key, rag_doc = await sync_lab_profile_rag(
        session,
        embedder=embedder,
        actor_id=actor_id,
        job=job,
    )

    merge_stats: dict[str, object] = {
        "added_entities": added_count,
        "updated_fields": updated_fields_count,
        "kept_fields": kept_fields_count,
        "forced_fields": forced_fields_count,
        "rag_doc_key": doc_key,
    }
    cur_stats.update(merge_stats)
    job.stats = cur_stats
    if rag_doc is not None:
        job.rag_document_id = rag_doc.id
    await session.flush()

    return {
        "job_id": str(job.id),
        "status": "merged",
        "as_of_time": as_of_date.isoformat(),
        "rag_document_id": str(rag_doc.id) if rag_doc is not None else None,
        "rag_doc_key": doc_key,
        "stats": merge_stats,
    }


async def revert_import_merge(
    session: AsyncSession,
    *,
    job_id: uuid.UUID,
    actor_id: uuid.UUID,
    embedder: Embedder,
) -> dict[str, object]:
    """Revert a merged ImportJob, recomputing effective field values and cleaning up orphan entities (Spec §10.6)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    job = await session.scalar(
        select(ImportJob).where(ImportJob.id == job_id).with_for_update()
    )
    if job is None:
        raise ProvisionError(404, "not_found", "导入任务不存在")
    if job.status not in ("merged", "provisioned"):
        raise ProvisionError(
            409, "import_not_merged", "该导入任务当前状态不可撤销合并"
        )

    job_prov_rows = list(
        await session.scalars(
            select(FieldProvenance).where(
                FieldProvenance.source_job_id == job.id,
                FieldProvenance.superseded.is_(False),
            )
        )
    )
    affected_entities: set[tuple[EntityType, uuid.UUID]] = set()
    for r in job_prov_rows:
        r.superseded = True
        if r.entity_type in MANAGED_FIELDS:
            affected_entities.add((r.entity_type, r.entity_id))  # type: ignore[arg-type]

    model_map: dict[EntityType, Any] = {
        "person": LabPerson,
        "project": LabProject,
        "achievement": LabAchievement,
        "facility": LabFacility,
    }
    for etype, model_cls in model_map.items():
        created_rows = list(
            await session.scalars(
                select(model_cls).where(model_cls.source_import_id == job.id)
            )
        )
        for ent in created_rows:
            affected_entities.add((etype, ent.id))

    await session.flush()

    reverted_fields = 0
    deleted_entities = 0

    for etype, eid in affected_entities:
        model_cls = model_map[etype]
        ent = await session.get(model_cls, eid)
        if ent is None:
            continue

        remaining_live = list(
            await session.scalars(
                select(FieldProvenance).where(
                    FieldProvenance.entity_type == etype,
                    FieldProvenance.entity_id == eid,
                    FieldProvenance.superseded.is_(False),
                )
            )
        )

        prior_affiliation = getattr(ent, "affiliation", None)
        prior_email = getattr(ent, "email", None)
        if ent.source_import_id == job.id and not remaining_live:
            if etype == "person" and ent.user_id is not None:
                # Spec §10.6: Never delete LabPerson linked to a User account!
                for field in MANAGED_FIELDS[etype]:
                    cur_val = get_entity_json_value(etype, ent, field)
                    if not _is_blank(cur_val) and field != "project_role":
                        set_entity_field_from_json(etype, ent, field, None)
                        reverted_fields += 1
                await _refresh_and_validate_entity_fingerprint(
                    session,
                    etype,
                    ent,
                    prior_affiliation=prior_affiliation,
                    prior_email=prior_email,
                )
            else:
                await session.delete(ent)
                deleted_entities += 1
            continue

        by_field: dict[str, list[FieldProvenance]] = {}
        for r in remaining_live:
            by_field.setdefault(r.field, []).append(r)

        for field in MANAGED_FIELDS[etype]:
            cur_val = get_entity_json_value(etype, ent, field)
            eff = effective_value(by_field.get(field, []))
            new_val = eff.value if eff is not None else None
            if new_val != cur_val:
                set_entity_field_from_json(etype, ent, field, new_val)
                reverted_fields += 1

        await _refresh_and_validate_entity_fingerprint(
            session,
            etype,
            ent,
            prior_affiliation=prior_affiliation,
            prior_email=prior_email,
        )

        rem_dates = [r.as_of_time for r in remaining_live if r.as_of_time is not None]
        if rem_dates:
            ent.as_of_time = max(rem_dates)

        if ent.source_import_id == job.id:
            rem_jobs = [
                r.source_job_id
                for r in remaining_live
                if r.source_job_id is not None and r.source_job_id != job.id
            ]
            if rem_jobs:
                ent.source_import_id = rem_jobs[-1]

    cur_stats = dict(job.stats or {})
    pre_status = cur_stats.get("pre_merge_status")
    restored_status = (
        str(pre_status)
        if isinstance(pre_status, str) and pre_status in ("parsed", "provisioned")
        else ("provisioned" if job.provisioned_at is not None else "parsed")
    )
    job.status = restored_status
    await session.flush()

    doc_key, _ = await sync_lab_profile_rag(
        session,
        embedder=embedder,
        actor_id=actor_id,
        job=None,
    )
    await session.flush()

    return {
        "job_id": str(job.id),
        "status": restored_status,
        "job_status": restored_status,
        "reverted_fields": reverted_fields,
        "deleted_entities": deleted_entities,
        "rag_doc_key": doc_key,
    }
