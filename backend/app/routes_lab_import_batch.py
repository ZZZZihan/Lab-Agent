"""Batch cold-start identity resolution and multi-proposal transactional provisioning routes."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import Actor
from app.db import get_session
from app.importer.batch_identity import resolve_batch_identities
from app.importer.fingerprint import norm
from app.importer.people import normalize_person_name
from app.importer.pii import PIILeakError, assert_no_pii
from app.importer.provision import (
    PersonOverride,
    ProvisionError,
    ProvisionRequest,
    _parse_iso_date,
    provision_import_job,
    render_roster_csv,
    render_roster_text,
    sync_lab_profile_rag,
)
from app.models import ImportJob, LabPerson, User
from app.routes import APIError, _actor, _client_ip, _embedder, _settings
from app.routes_admin import _require_admin
from app.routes_lab_schemas import _serialize_import_job

router = APIRouter()


class BatchResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_ids: list[uuid.UUID] = Field(default_factory=list)


class BatchJobProvisionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: uuid.UUID
    as_of_time: date | None = None
    people: list[PersonOverride] = Field(default_factory=list)
    exclude_keys: list[str] = Field(default_factory=list)
    skip_signature_ids: list[str] = Field(default_factory=list)


class BatchProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_ids: list[uuid.UUID] = Field(default_factory=list)
    initial_password: str | None = None
    include_password: bool = True
    link_self_cluster_id: str | None = None
    jobs: list[BatchJobProvisionItem] = Field(default_factory=list)


@router.post("/admin/imports/batch/resolve")
async def batch_resolve_imports(
    body: BatchResolveRequest,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="import_batch_resolve",
        resource_type="import_job",
    )
    dedup_ids = list(dict.fromkeys(body.job_ids))
    if not dedup_ids or len(dedup_ids) > 5 or len(body.job_ids) > 5:
        raise APIError(
            422,
            "batch_size_invalid",
            "单次批量冷启动仅支持 1~5 份申报书",
        )

    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    jobs: list[ImportJob] = []
    for jid in dedup_ids:
        job = await session.scalar(
            select(ImportJob).where(ImportJob.id == jid).with_for_update()
        )
        if job is None:
            raise APIError(404, "not_found", f"导入任务不存在: {jid}")
        if job.status != "parsed":
            raise APIError(
                409,
                "import_not_parsed",
                f"导入任务 {jid} 当前状态不可解析身份 ({job.status})",
            )
        jobs.append(job)

    existing_users = list(await session.scalars(select(User)))
    existing_people = list(await session.scalars(select(LabPerson)))
    actor_user = next((u for u in existing_users if u.id == actor.id), None)

    resolved = resolve_batch_identities(
        jobs,
        existing_users=existing_users,
        existing_people=existing_people,
        actor_user=actor_user,
    )

    serialized_jobs: list[dict[str, object]] = []
    for job in jobs:
        try:
            assert_no_pii(job.preview or {})
        except PIILeakError as exc:
            await session.rollback()
            raise APIError(422, "pii_detected", str(exc)) from exc
        serialized_jobs.append(_serialize_import_job(job, include_preview=True))

    await session.flush()
    q = resolved["quality"]
    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="import_batch_resolve",
        resource_type="import_job",
        resource_id=str(jobs[0].id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "job_ids": [str(j.id) for j in jobs[:80]],
            "clusters": len(resolved["clusters"]),
            "people_total": int(q["people_total"]),
            "t1_covered": int(q["t1_covered"]),
            "injected_student_ids": int(q["injected_student_ids"]),
            "injected_emails": int(q["injected_emails"]),
        },
        session=session,
    )
    await session.commit()
    return {
        "jobs": serialized_jobs,
        "clusters": resolved["clusters"],
        "quality": resolved["quality"],
        "self_candidate": resolved["self_candidate"],
    }


@router.post("/admin/imports/batch/provision")
async def batch_provision_imports(
    body: BatchProvisionRequest,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="import_batch_provision",
        resource_type="import_job",
    )
    raw_ids: list[uuid.UUID] = list(body.job_ids)
    for item in body.jobs:
        if item.job_id not in raw_ids:
            raw_ids.append(item.job_id)
    target_job_ids = list(dict.fromkeys(raw_ids))
    if (
        not target_job_ids
        or len(target_job_ids) > 5
        or len(body.job_ids) > 5
        or len(body.jobs) > 5
    ):
        raise APIError(
            422,
            "batch_size_invalid",
            "单次批量冷启动仅支持 1~5 份申报书",
        )

    settings = _settings(request)
    embedder = _embedder(request)
    oidc_mode = bool(settings.oidc_issuer and settings.oidc_issuer.strip())
    if settings.app_env == "prod" and not oidc_mode:
        if (
            not body.initial_password
            or not body.initial_password.strip()
            or body.initial_password.strip() == settings.import_default_password
        ):
            raise APIError(
                422,
                "initial_password_required",
                "生产环境下批量开号必须填写自定义初始口令，禁止使用默认公开口令",
            )

    await session.execute(text("SELECT pg_advisory_xact_lock(2026111008)"))
    jobs: list[ImportJob] = []
    for jid in target_job_ids:
        job = await session.scalar(
            select(ImportJob).where(ImportJob.id == jid).with_for_update()
        )
        if job is None:
            raise APIError(404, "not_found", f"导入任务不存在: {jid}")
        if job.status == "provisioned":
            raise APIError(
                409, "import_already_provisioned", f"导入任务 {jid} 已完成开号"
            )
        if job.status != "parsed":
            raise APIError(
                409,
                "import_not_parsed",
                f"导入任务 {jid} 当前状态不可开号 ({job.status})",
            )
        jobs.append(job)

    items_by_job_id: dict[uuid.UUID, BatchJobProvisionItem] = {
        item.job_id: BatchJobProvisionItem(
            job_id=item.job_id,
            as_of_time=item.as_of_time,
            people=[
                PersonOverride(
                    key=ov.key,
                    exclude=ov.exclude,
                    login_name=ov.login_name,
                    link_user_id=ov.link_user_id,
                )
                for ov in item.people
            ],
            exclude_keys=list(item.exclude_keys),
            skip_signature_ids=list(item.skip_signature_ids),
        )
        for item in body.jobs
    }
    for jid in target_job_ids:
        if jid not in items_by_job_id:
            items_by_job_id[jid] = BatchJobProvisionItem(job_id=jid)

    exclude_keys_by_job: dict[uuid.UUID, set[str]] = {}
    for jid, item in items_by_job_id.items():
        ex_set = {str(k).strip() for k in item.exclude_keys if str(k).strip()}
        for ov in item.people:
            if ov.exclude and ov.key.strip():
                ex_set.add(ov.key.strip())
        exclude_keys_by_job[jid] = ex_set

    existing_users = list(await session.scalars(select(User)))
    existing_people = list(await session.scalars(select(LabPerson)))
    actor_user = next((u for u in existing_users if u.id == actor.id), None)

    has_any_exclusions = any(bool(s) for s in exclude_keys_by_job.values())
    if has_any_exclusions:
        has_prior_cids = True
        for j in jobs:
            j_ppl = (j.preview or {}).get("people") if isinstance(j.preview, dict) else None
            if isinstance(j_ppl, list) and j_ppl:
                if not any(isinstance(p, dict) and bool(p.get("cluster_id")) for p in j_ppl):
                    has_prior_cids = False
                    break
        if not has_prior_cids:
            resolve_batch_identities(
                jobs,
                existing_users=existing_users,
                existing_people=existing_people,
                actor_user=actor_user,
                exclude_keys_by_job=None,
            )

    resolved = resolve_batch_identities(
        jobs,
        existing_users=existing_users,
        existing_people=existing_people,
        actor_user=actor_user,
        exclude_keys_by_job=exclude_keys_by_job,
    )

    if body.link_self_cluster_id and body.link_self_cluster_id.strip():
        target_cid = body.link_self_cluster_id.strip()
        matched_cluster = next(
            (
                c
                for c in resolved["clusters"]
                if str(c.get("cluster_id") or "") == target_cid
            ),
            None,
        )
        actor_norm_name = (
            norm(normalize_person_name(actor_user.display_name))
            if actor_user is not None
            else ""
        )
        cluster_norm_name = (
            norm(normalize_person_name(str(matched_cluster.get("name") or "")))
            if matched_cluster is not None
            else ""
        )
        if (
            matched_cluster is None
            or actor_user is None
            or any(lp.user_id == actor.id for lp in existing_people)
            or (
                matched_cluster.get("matched_user_id") is not None
                and str(matched_cluster.get("matched_user_id")) != str(actor.id)
            )
            or not actor_norm_name
            or cluster_norm_name != actor_norm_name
        ):
            await session.rollback()
            raise APIError(
                422,
                "link_self_cluster_invalid",
                f"指定的本人关联聚类无效或与当前管理员身份不匹配: {target_cid}",
            )
        for member in matched_cluster.get("members", []):
            if not isinstance(member, dict):
                continue
            m_jid = uuid.UUID(str(member["job_id"]))
            m_key = str(
                member.get("key")
                or member.get("person_key")
                or member.get("temp_id")
                or ""
            ).strip()
            if not m_key:
                continue
            item_cfg = items_by_job_id[m_jid]
            if m_key in exclude_keys_by_job.get(m_jid, set()):
                continue
            existing_ov = next(
                (ov for ov in item_cfg.people if ov.key == m_key), None
            )
            if existing_ov is not None:
                if not existing_ov.exclude and existing_ov.link_user_id is None:
                    existing_ov.link_user_id = actor.id
            else:
                item_cfg.people.append(
                    PersonOverride(key=m_key, link_user_id=actor.id)
                )

    indexed_jobs: list[tuple[int, date, ImportJob]] = []
    for orig_idx, job in enumerate(jobs):
        item_cfg = items_by_job_id[job.id]
        preview = dict(job.preview or {})
        parsed_preview_date = _parse_iso_date(preview.get("as_of_time"))
        resolved_as_of = item_cfg.as_of_time or job.as_of_time or parsed_preview_date
        if resolved_as_of is None:
            job_fname = job.original_filename
            job_id_str = str(job.id)
            await session.rollback()
            raise APIError(
                422,
                "as_of_time_required",
                f"请提供本子 {job_fname} ({job_id_str}) 的基准日期 as_of_time",
            )
        indexed_jobs.append((orig_idx, resolved_as_of, job))

    sorted_entries = sorted(indexed_jobs, key=lambda x: (x[1], x[0]))
    sorted_jobs = [entry[2] for entry in sorted_entries]

    cluster_by_job_person: dict[tuple[uuid.UUID, str], str] = {}
    cluster_members_by_cid: dict[str, list[tuple[uuid.UUID, str]]] = {}
    cluster_custom_login_by_cid: dict[str, str | None] = {}
    for cl in resolved["clusters"]:
        cid = str(cl.get("cluster_id") or "")
        if not cid:
            continue
        cluster_custom_logins: set[str] = set()
        cluster_members_keys: list[tuple[uuid.UUID, str]] = []
        for member in cl.get("members", []):
            if not isinstance(member, dict):
                continue
            try:
                m_jid = uuid.UUID(str(member["job_id"]))
            except (KeyError, ValueError):
                continue
            m_key = str(
                member.get("key")
                or member.get("person_key")
                or member.get("temp_id")
                or ""
            ).strip()
            if m_key:
                cluster_by_job_person[(m_jid, m_key)] = cid
                if m_key not in exclude_keys_by_job.get(m_jid, set()):
                    cluster_members_keys.append((m_jid, m_key))
                    m_cfg = items_by_job_id.get(m_jid)
                    if m_cfg is not None:
                        m_ov = next(
                            (ov for ov in m_cfg.people if ov.key == m_key), None
                        )
                        if (
                            m_ov is not None
                            and not m_ov.exclude
                            and m_ov.login_name is not None
                            and m_ov.login_name.strip()
                        ):
                            cluster_custom_logins.add(
                                m_ov.login_name.strip().lower()
                            )
        if len(cluster_custom_logins) > 1:
            await session.rollback()
            raise APIError(
                422,
                "login_name_invalid",
                f"同一人员聚类 ({cid}) 在不同本子中设置了冲突的自定义登录名",
            )
        shared_login = (
            next(iter(cluster_custom_logins))
            if len(cluster_custom_logins) == 1
            else None
        )
        cluster_members_by_cid[cid] = cluster_members_keys
        cluster_custom_login_by_cid[cid] = shared_login
        if shared_login is not None:
            for m_jid, m_key in cluster_members_keys:
                m_cfg = items_by_job_id[m_jid]
                m_ov = next(
                    (ov for ov in m_cfg.people if ov.key == m_key), None
                )
                if m_ov is not None:
                    if not m_ov.exclude and m_ov.login_name is None:
                        m_ov.login_name = shared_login
                else:
                    m_cfg.people.append(
                        PersonOverride(key=m_key, login_name=shared_login)
                    )

    jobs_by_id = {j.id: j for j in jobs}
    cluster_has_decision: dict[str, bool] = {}
    for cl in resolved["clusters"]:
        cid = str(cl.get("cluster_id") or "")
        if not cid:
            continue
        has_dec = bool(cluster_custom_login_by_cid.get(cid))
        if not has_dec:
            for m_jid, m_key in cluster_members_by_cid.get(cid, []):
                m_cfg = items_by_job_id.get(m_jid)
                if m_cfg is not None:
                    m_ov = next((ov for ov in m_cfg.people if ov.key == m_key), None)
                    if m_ov is not None and not m_ov.exclude and m_ov.link_user_id is not None:
                        has_dec = True
                        break
        cluster_has_decision[cid] = has_dec

    for cl in resolved["clusters"]:
        cid = str(cl.get("cluster_id") or "")
        if not cid:
            continue
        cl_norm_name = norm(normalize_person_name(str(cl.get("name") or "")))
        cl_sid = str(cl.get("student_id") or "").strip().lower() or None
        cl_em = str(cl.get("email") or "").strip().lower() or None
        conflicting_other_cids: list[str] = []
        if cl_sid or cl_em:
            for other in resolved["clusters"]:
                other_cid = str(other.get("cluster_id") or "")
                if not other_cid or other_cid == cid:
                    continue
                if norm(normalize_person_name(str(other.get("name") or ""))) != cl_norm_name:
                    continue
                other_sid = str(other.get("student_id") or "").strip().lower() or None
                other_em = str(other.get("email") or "").strip().lower() or None
                if (
                    (cl_sid and other_sid and cl_sid == other_sid)
                    or (cl_em and other_em and cl_em == other_em)
                    or (not cl_sid and other_sid and not other_em)
                ):
                    conflicting_other_cids.append(other_cid)

        if conflicting_other_cids and (
            cluster_has_decision.get(cid, False)
            or all(
                cluster_has_decision.get(ocid, False)
                for ocid in conflicting_other_cids
            )
        ):
            for m_jid, m_key in cluster_members_by_cid.get(cid, []):
                if m_jid in jobs_by_id:
                    j_obj = jobs_by_id[m_jid]
                    j_prev = dict(j_obj.preview or {})
                    j_ppl = j_prev.get("people")
                    if isinstance(j_ppl, list):
                        for p in j_ppl:
                            if isinstance(p, dict) and str(p.get("key") or p.get("temp_id") or "").strip() == m_key:
                                p["collision_resolved_in_batch"] = True
                                p["collision_ambiguous"] = False
                        j_obj.preview = j_prev

        if str(cl.get("tier") or "T2") not in ("T1", "T2", "T3"):
            continue
        custom_login = cluster_custom_login_by_cid.get(cid)
        if not custom_login:
            continue
        if not cl_norm_name:
            continue
        target_cl = next(
            (
                other
                for other in resolved["clusters"]
                if str(other.get("cluster_id") or "") not in ("", cid)
                and norm(normalize_person_name(str(other.get("name") or ""))) == cl_norm_name
                and (
                    (
                        cluster_custom_login_by_cid.get(str(other.get("cluster_id") or ""))
                        or str(other.get("login_name") or "").strip().lower()
                    )
                    == custom_login
                    or str(other.get("student_id") or "").strip().lower() == custom_login
                )
            ),
            None,
        )
        if target_cl is not None:
            target_cid = str(target_cl["cluster_id"])
            target_sid = str(target_cl.get("student_id") or "").strip().lower() or None
            target_aff = str(target_cl.get("affiliation") or "").strip() or None
            for m_jid, m_key in cluster_members_by_cid.get(cid, []):
                cluster_by_job_person[(m_jid, m_key)] = target_cid
                if (target_sid or target_aff) and m_jid in jobs_by_id:
                    j_obj = jobs_by_id[m_jid]
                    j_prev = dict(j_obj.preview or {})
                    j_ppl = j_prev.get("people")
                    if isinstance(j_ppl, list):
                        for p in j_ppl:
                            if isinstance(p, dict) and str(p.get("key") or p.get("temp_id") or "").strip() == m_key:
                                if target_sid:
                                    p["student_id"] = target_sid
                                    p["cert"] = target_sid
                                    p["tier"] = "T1"
                                if target_aff and not str(p.get("affiliation") or "").strip():
                                    p["affiliation"] = target_aff
                        j_obj.preview = j_prev

    cluster_resolved_user_id: dict[str, uuid.UUID] = {}
    job_results: list[dict[str, Any]] = []
    for _, resolved_as_of, job in sorted_entries:
        job_id_val = job.id
        job_fname = job.original_filename
        item_cfg = items_by_job_id[job_id_val]
        ov_by_key = {ov.key: ov for ov in item_cfg.people}
        preview_ppl = (job.preview or {}).get("people") if isinstance(job.preview, dict) else []
        for p in (preview_ppl if isinstance(preview_ppl, list) else []):
            if not isinstance(p, dict):
                continue
            pkey = str(p.get("key") or p.get("temp_id") or "").strip()
            if not pkey or pkey in exclude_keys_by_job.get(job_id_val, set()):
                continue
            person_cid = cluster_by_job_person.get((job_id_val, pkey))
            if person_cid and person_cid in cluster_resolved_user_id:
                resolved_uid = cluster_resolved_user_id[person_cid]
                existing_ov = ov_by_key.get(pkey)
                if existing_ov is not None:
                    if not existing_ov.exclude and existing_ov.link_user_id is None:
                        existing_ov.link_user_id = resolved_uid
                else:
                    new_ov = PersonOverride(key=pkey, link_user_id=resolved_uid)
                    item_cfg.people.append(new_ov)
                    ov_by_key[pkey] = new_ov

        single_req = ProvisionRequest(
            as_of_time=resolved_as_of,
            initial_password=body.initial_password,
            include_password=body.include_password,
            people=item_cfg.people,
            skip_signature_ids=item_cfg.skip_signature_ids,
            exclude_keys=item_cfg.exclude_keys,
        )
        try:
            res = await provision_import_job(
                session,
                job_id=job_id_val,
                actor_id=actor.id,
                body=single_req,
                settings=settings,
                embedder=embedder,
                sync_rag=False,
            )
        except ProvisionError as exc:
            await session.rollback()
            raise APIError(
                exc.status,
                exc.code,
                f"[{job_fname} ({job_id_val})] {exc.message}",
            ) from exc

        updated_ppl = (job.preview or {}).get("people") if isinstance(job.preview, dict) else []
        for p in (updated_ppl if isinstance(updated_ppl, list) else []):
            if not isinstance(p, dict):
                continue
            pkey = str(p.get("key") or p.get("temp_id") or "").strip()
            person_cid = cluster_by_job_person.get((job_id_val, pkey))
            raw_match = p.get("match")
            prov_uid_raw = p.get("provisioned_user_id") or (
                raw_match.get("user_id") if isinstance(raw_match, dict) else None
            )
            if (
                person_cid
                and person_cid not in cluster_resolved_user_id
                and prov_uid_raw
            ):
                try:
                    cluster_resolved_user_id[person_cid] = uuid.UUID(str(prov_uid_raw))
                except ValueError:
                    pass

        job_results.append(
            {
                **res,
                "id": str(job_id_val),
                "original_filename": job_fname,
            }
        )

    doc_key, rag_doc = await sync_lab_profile_rag(
        session,
        embedder=embedder,
        actor_id=actor.id,
        job=sorted_jobs[-1],
    )
    rag_doc_id_str = str(rag_doc.id) if rag_doc is not None else None
    for job in sorted_jobs:
        if rag_doc is not None:
            job.rag_document_id = rag_doc.id
        st = dict(job.stats or {})
        st["rag_doc_key"] = doc_key
        job.stats = st

    for jr in job_results:
        jr["rag_document_id"] = rag_doc_id_str
        jr["rag_doc_key"] = doc_key
        if isinstance(jr.get("stats"), dict):
            jr["stats"]["rag_doc_key"] = doc_key

    # Deduplicate combined roster across all jobs in the batch by user_id
    created_user_ids: set[str] = set()
    seen_existing_user_ids: set[str] = set()
    combined_new_accounts: list[dict[str, object]] = []
    combined_existing_accounts: list[dict[str, object]] = []

    for jr in job_results:
        raw_stats = jr.get("stats")
        stats_dict = raw_stats if isinstance(raw_stats, dict) else {}
        roster_rows = stats_dict.get("roster_rows")
        for row in (roster_rows if isinstance(roster_rows, list) else []):
            if not isinstance(row, dict):
                continue
            uid = str(row.get("user_id") or row.get("login_name") or row.get("name") or "")
            acc_status = str(row.get("account_status") or "new")
            if acc_status == "new":
                if uid not in created_user_ids:
                    created_user_ids.add(uid)
                    combined_new_accounts.append(row)
            else:
                if uid in created_user_ids or uid in seen_existing_user_ids:
                    continue
                seen_existing_user_ids.add(uid)
                combined_existing_accounts.append(row)

    initial_password = (
        body.initial_password.strip()
        if body.initial_password and body.initial_password.strip()
        else settings.import_default_password
    )
    now_utc = datetime.now(UTC)
    deadline_utc = now_utc + timedelta(days=max(1, settings.initial_password_ttl_days))
    try:
        tz = ZoneInfo(settings.display_tz)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    deadline_display = deadline_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")

    roster_text = render_roster_text(
        base_url=settings.public_base_url,
        initial_password=None if oidc_mode else initial_password,
        include_password=body.include_password,
        deadline_str=deadline_display,
        new_accounts=combined_new_accounts,
        existing_accounts=combined_existing_accounts,
        oidc_mode=oidc_mode,
    )
    roster_csv = render_roster_csv(
        initial_password=None if oidc_mode else initial_password,
        include_password=body.include_password,
        deadline_str=deadline_display,
        new_accounts=combined_new_accounts,
        existing_accounts=combined_existing_accounts,
        oidc_mode=oidc_mode,
    )

    merge_queue = [str(j.id) for j in sorted_jobs[1:]]

    def _sum_stat(field_name: str) -> int:
        total = 0
        for jr in job_results:
            st_obj = jr.get("stats")
            if isinstance(st_obj, dict):
                val = st_obj.get(field_name)
                if isinstance(val, int):
                    total += val
        return total

    aggregated_stats: dict[str, object] = {
        "users_created": len(combined_new_accounts),
        "users_linked": len(combined_existing_accounts),
        "people_inserted": _sum_stat("people_inserted"),
        "people_skipped": _sum_stat("people_skipped"),
        "achievements_inserted": _sum_stat("achievements_inserted"),
        "achievements_skipped": _sum_stat("achievements_skipped"),
        "projects_inserted": _sum_stat("projects_inserted"),
        "projects_skipped": _sum_stat("projects_skipped"),
        "facilities_inserted": _sum_stat("facilities_inserted"),
        "facilities_skipped": _sum_stat("facilities_skipped"),
        "rag_doc_key": doc_key,
    }

    await session.flush()
    await write_audit(
        settings=settings,
        actor_id=actor.id,
        action="import_batch_provision",
        resource_type="import_job",
        resource_id=str(sorted_jobs[-1].id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "job_ids": [str(j.id) for j in sorted_jobs[:80]],
            "users_created": len(combined_new_accounts),
            "users_linked": len(combined_existing_accounts),
            "merge_queue": merge_queue[:80],
            "include_password": bool(body.include_password),
        },
        session=session,
    )
    await session.commit()

    return {
        "jobs": job_results,
        "stats": aggregated_stats,
        "roster_text": roster_text,
        "roster_csv": roster_csv,
        "deadline": None if oidc_mode else deadline_utc.isoformat(),
        "initial_password": (
            None if (oidc_mode or not body.include_password) else initial_password
        ),
        "rag_document_id": rag_doc_id_str,
        "rag_doc_key": doc_key,
        "merge_queue": merge_queue,
    }
