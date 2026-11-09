"""Cross-proposal person clustering and T1 student/staff ID & email enrichment for batch cold-start."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from app.importer.fingerprint import norm
from app.importer.people import (
    RESERVED_LOGIN_NAMES,
    T1_CERT_RE,
    assign_people_identities,
    clean_email,
    name_to_pinyin,
    normalize_person_name,
)
from app.importer.pii import is_pii_id_or_phone
from app.importer.provision import _parse_iso_date
from app.models import ImportJob, LabPerson, User


def _is_person_excluded(
    p: dict[str, Any], exclude_keys: set[str] | None = None
) -> bool:
    """Return True if a person entry in preview['people'] is excluded from import."""
    if exclude_keys:
        k1 = str(p.get("key") or "").strip()
        k2 = str(p.get("temp_id") or "").strip()
        if (k1 and k1 in exclude_keys) or (k2 and k2 in exclude_keys):
            return True
    return bool(
        p.get("exclude") is True
        or p.get("excluded") is True
        or p.get("include") is False
        or p.get("imported") is False
        or p.get("override_mode") == "exclude"
    )


def _is_suffixed_pinyin_login(cand_sid: str, raw_name: str) -> bool:
    """Return True if `cand_sid` is a system-generated suffixed pinyin login (e.g. `zhangsan2`)."""
    if not cand_sid:
        return False
    base = name_to_pinyin(raw_name)
    if not base:
        return False
    return bool(re.match(rf"^{re.escape(base)}\d+$", cand_sid.strip().lower()))


def _reset_sibling_injections(p: dict[str, Any]) -> None:
    """Strip any previously injected sibling fields and collision warnings so `resolve` is idempotent."""
    p.pop("cluster_id", None)
    p.pop("provisioned_user_id", None)
    p.pop("collision_ambiguous", None)
    raw_warnings = p.get("warnings")
    warnings = [
        str(w)
        for w in (raw_warnings if isinstance(raw_warnings, list) else [])
        if isinstance(w, str)
    ]
    warnings = [w for w in warnings if w != "BATCH_NAME_COLLISION"]

    if "STUDENT_ID_FROM_SIBLING" in warnings or p.get("student_id_source"):
        warnings = [w for w in warnings if w != "STUDENT_ID_FROM_SIBLING"]
        p.pop("student_id", None)
        p.pop("student_id_source", None)
        p.pop("cert", None)
        if p.get("tier") == "T1":
            p["tier"] = "T3" if p.get("email") else "T2"
            p.pop("login_name", None)

    if "EMAIL_FROM_SIBLING" in warnings or p.get("email_source"):
        warnings = [w for w in warnings if w != "EMAIL_FROM_SIBLING"]
        p["email"] = None
        p.pop("email_source", None)
        if p.get("tier") == "T3":
            p["tier"] = "T2"

    p["warnings"] = warnings


def _extract_native_student_id(p: dict[str, Any]) -> str | None:
    """Extract a person's native T1 student/staff ID, ignoring suffixed pinyin logins and sibling injections."""
    raw_warnings = p.get("warnings")
    warnings = [
        str(w)
        for w in (raw_warnings if isinstance(raw_warnings, list) else [])
        if isinstance(w, str)
    ]
    if (
        "STUDENT_ID_FROM_SIBLING" in warnings
        or p.get("student_id_source")
        or "LOGIN_NAME_SUFFIXED" in warnings
    ):
        return None

    name = normalize_person_name(str(p.get("name") or ""))
    raw_tier = p.get("tier")
    effective_tier = (
        raw_tier
        if raw_tier in ("T1", "T2", "T3")
        else ("T1" if (p.get("student_id") or p.get("cert")) else "T2")
    )
    if effective_tier != "T1":
        return None

    cand_sid = (
        str(
            p.get("student_id")
            or (p.get("cert") if effective_tier == "T1" else "")
            or (
                p.get("login_name")
                if effective_tier == "T1" and "LOGIN_NAME_SUFFIXED" not in warnings
                else ""
            )
            or ""
        )
        .strip()
        .lower()
    )
    if (
        cand_sid
        and bool(T1_CERT_RE.match(cand_sid))
        and not is_pii_id_or_phone(cand_sid)
        and "LOGIN_NAME_SUFFIXED" not in warnings
        and not _is_suffixed_pinyin_login(cand_sid, name)
    ):
        p["tier"] = "T1"
        p["student_id"] = cand_sid
        return cand_sid
    return None


def _extract_native_email(p: dict[str, Any]) -> str | None:
    """Extract a person's native email, ignoring sibling-injected emails."""
    raw_warnings = p.get("warnings")
    warnings = [
        str(w)
        for w in (raw_warnings if isinstance(raw_warnings, list) else [])
        if isinstance(w, str)
    ]
    if "EMAIL_FROM_SIBLING" in warnings or p.get("email_source"):
        return None
    raw_em = p.get("email")
    return clean_email(str(raw_em) if raw_em else None)


def _effective_job_as_of(job: ImportJob) -> date | None:
    if job.as_of_time is not None:
        return job.as_of_time
    preview = job.preview if isinstance(job.preview, dict) else {}
    return _parse_iso_date(preview.get("as_of_time"))


class _Occurrence:
    __slots__ = (
        "affiliation",
        "job",
        "job_id",
        "job_idx",
        "name",
        "native_email",
        "native_sid",
        "norm_aff",
        "norm_name",
        "person",
        "person_key",
        "prior_cluster_id",
    )

    def __init__(
        self,
        *,
        job: ImportJob,
        job_idx: int,
        person: dict[str, Any],
        person_key: str,
        name: str,
        norm_name: str,
        affiliation: str,
        norm_aff: str,
        native_sid: str | None,
        native_email: str | None,
        prior_cluster_id: str | None = None,
    ) -> None:
        self.job = job
        self.job_id = job.id
        self.job_idx = job_idx
        self.person = person
        self.person_key = person_key
        self.name = name
        self.norm_name = norm_name
        self.affiliation = affiliation
        self.norm_aff = norm_aff
        self.native_sid = native_sid
        self.native_email = native_email
        self.prior_cluster_id = prior_cluster_id


def resolve_batch_identities(
    jobs: Sequence[ImportJob],
    *,
    existing_users: list[User] | None = None,
    existing_people: list[LabPerson] | None = None,
    actor_user: User | None = None,
    exclude_keys_by_job: dict[uuid.UUID, set[str]] | None = None,
) -> dict[str, Any]:
    """Cluster people across 1..5 ImportJobs, enrich sibling T1 IDs & emails, and compute quality metrics."""
    users = list(existing_users or [])
    people_db = list(existing_people or [])
    users_by_id = {u.id: u for u in users}

    job_people_lists: dict[uuid.UUID, list[dict[str, Any]]] = {}
    job_previews: dict[uuid.UUID, dict[str, Any]] = {}
    per_job_name_counts: dict[uuid.UUID, dict[str, int]] = {}
    prior_cid_by_job_key: dict[tuple[uuid.UUID, str], str] = {}
    occurrences: list[_Occurrence] = []

    # Pre-pass: reset prior sibling injections and count normalized names per job among non-excluded people
    for job in jobs:
        preview = dict(job.preview or {})
        job_previews[job.id] = preview
        raw_ppl = preview.get("people")
        ppl_list: list[dict[str, Any]] = [
            dict(p)
            for p in (raw_ppl if isinstance(raw_ppl, list) else [])
            if isinstance(p, dict)
        ]
        job_people_lists[job.id] = ppl_list
        ex_keys = (exclude_keys_by_job or {}).get(job.id)
        name_counts: dict[str, int] = {}
        per_job_name_counts[job.id] = name_counts

        for idx, p in enumerate(ppl_list, start=1):
            pkey = str(p.get("key") or p.get("temp_id") or f"p{idx}").strip()
            p["key"] = pkey
            p["temp_id"] = pkey
            raw_prior_cid = str(p.get("cluster_id") or "").strip()
            if raw_prior_cid:
                prior_cid_by_job_key[(job.id, pkey)] = raw_prior_cid
            _reset_sibling_injections(p)
            if _is_person_excluded(p, ex_keys):
                if ex_keys and pkey in ex_keys:
                    p["exclude"] = True
                continue
            pname = normalize_person_name(str(p.get("name") or ""))
            p["name"] = pname
            n_name = norm(pname)
            if n_name:
                name_counts[n_name] = name_counts.get(n_name, 0) + 1

    # Build non-excluded occurrences using the exact same _is_person_excluded guard
    for job_idx, job in enumerate(jobs):
        ppl_list = job_people_lists[job.id]
        ex_keys = (exclude_keys_by_job or {}).get(job.id)
        for idx, p in enumerate(ppl_list, start=1):
            if _is_person_excluded(p, ex_keys):
                continue
            pkey = str(p.get("key") or p.get("temp_id") or f"p{idx}").strip()
            pname = normalize_person_name(str(p.get("name") or ""))
            n_name = norm(pname)
            if not n_name:
                continue
            aff = str(p.get("affiliation") or "").strip()
            n_aff = norm(aff)
            native_sid = _extract_native_student_id(p)
            native_email = _extract_native_email(p)
            occurrences.append(
                _Occurrence(
                    job=job,
                    job_idx=job_idx,
                    person=p,
                    person_key=pkey,
                    name=pname,
                    norm_name=n_name,
                    affiliation=aff,
                    norm_aff=n_aff,
                    native_sid=native_sid,
                    native_email=native_email,
                    prior_cluster_id=prior_cid_by_job_key.get((job.id, pkey)),
                )
            )

    # Detect homonym / name collisions before merging by name
    occs_by_name: dict[str, list[_Occurrence]] = {}
    for occ in occurrences:
        occs_by_name.setdefault(occ.norm_name, []).append(occ)

    name_collisions: list[str] = []
    collided_norm_names: set[str] = set()

    def _mark_collision(occ_a: _Occurrence, occ_b: _Occurrence | None = None) -> None:
        if occ_a.norm_name not in collided_norm_names:
            collided_norm_names.add(occ_a.norm_name)
        if occ_a.name and occ_a.name not in name_collisions:
            name_collisions.append(occ_a.name)
        for target in (occ_a, occ_b):
            if target is None:
                continue
            w_list = list(target.person.get("warnings") or [])
            if "BATCH_NAME_COLLISION" not in w_list:
                w_list.append("BATCH_NAME_COLLISION")
            target.person["warnings"] = w_list

    for n_name, name_occs in occs_by_name.items():
        # Check within-job duplicate name
        for occ in name_occs:
            if per_job_name_counts[occ.job_id].get(n_name, 0) > 1:
                _mark_collision(occ)
        # Check pairwise conflicting native student_id or native email
        for i in range(len(name_occs)):
            for j in range(i + 1, len(name_occs)):
                oa = name_occs[i]
                ob = name_occs[j]
                if oa.native_sid and ob.native_sid and oa.native_sid != ob.native_sid:
                    _mark_collision(oa, ob)
                elif (
                    oa.native_email
                    and ob.native_email
                    and oa.native_email != ob.native_email
                ):
                    _mark_collision(oa, ob)

        if n_name in collided_norm_names:
            for occ in name_occs:
                _mark_collision(occ)

    # Clustering:
    # Step 1: Start each occurrence in its own cluster, merging exact T1 matches (same native_sid + same norm_name)
    raw_clusters: list[list[_Occurrence]] = []
    for occ in occurrences:
        merged_exact = False
        if occ.native_sid:
            for cl in raw_clusters:
                if cl[0].norm_name != occ.norm_name:
                    continue
                cl_sids = {o.native_sid for o in cl if o.native_sid}
                cl_ems = {o.native_email for o in cl if o.native_email}
                if cl_sids == {occ.native_sid} and not (
                    occ.native_email and cl_ems and cl_ems != {occ.native_email}
                ):
                    cl.append(occ)
                    merged_exact = True
                    break
        if not merged_exact:
            raw_clusters.append([occ])

    # Step 2: Merge clusters with the same norm_name if no collision and compatible affiliation
    merged_clusters: list[list[_Occurrence]] = []
    for cl in raw_clusters:
        n_name = cl[0].norm_name
        if n_name in collided_norm_names:
            merged_clusters.append(cl)
            continue

        distinct_affs_for_name = {
            o.norm_aff for o in occs_by_name.get(n_name, []) if o.norm_aff
        }
        did_merge = False
        for target_cl in merged_clusters:
            if target_cl[0].norm_name != n_name:
                continue
            combined = target_cl + cl
            # Every job in the merged cluster must have per_job_name_counts[job_id][n_name] == 1
            if any(
                per_job_name_counts[o.job_id].get(n_name, 0) != 1 for o in combined
            ):
                continue
            # No duplicate job_ids in the same cluster unless already merged by exact T1
            target_jobs = {o.job_id for o in target_cl}
            cl_jobs = {o.job_id for o in cl}
            if target_jobs & cl_jobs:
                continue
            # Check conflicting native student_ids or emails
            sids = {o.native_sid for o in combined if o.native_sid}
            if len(sids) > 1:
                continue
            ems = {o.native_email for o in combined if o.native_email}
            if len(ems) > 1:
                continue
            # Check affiliation compatibility
            affs_a = {o.norm_aff for o in target_cl if o.norm_aff}
            affs_b = {o.norm_aff for o in cl if o.norm_aff}
            if affs_a and affs_b and affs_a != affs_b:
                continue
            if (not affs_a or not affs_b) and len(distinct_affs_for_name) > 1:
                continue

            target_cl.extend(cl)
            did_merge = True
            break

        if not did_merge:
            merged_clusters.append(cl)

    clusters_by_norm_name: dict[str, list[list[_Occurrence]]] = {}
    for cl in merged_clusters:
        clusters_by_norm_name.setdefault(cl[0].norm_name, []).append(cl)
    for _n_name, cls_for_name in clusters_by_norm_name.items():
        if len(cls_for_name) > 1:
            for cl in cls_for_name:
                for occ in cl:
                    _mark_collision(occ)

    # Step 3: Cross-job sibling injection of student_id and email
    injected_student_ids = 0
    injected_emails = 0
    has_exclusions = bool(
        exclude_keys_by_job and any(bool(s) for s in exclude_keys_by_job.values())
    )
    assigned_cids: list[str] = [""] * len(merged_clusters)
    used_cids: set[str] = set()
    if has_exclusions:
        for i_cl, cl in enumerate(merged_clusters):
            prior_cids = {o.prior_cluster_id for o in cl if o.prior_cluster_id}
            if len(prior_cids) == 1:
                cand_cid = next(iter(prior_cids))
                if cand_cid and cand_cid not in used_cids:
                    assigned_cids[i_cl] = cand_cid
                    used_cids.add(cand_cid)
    next_seq = 1
    for i_cl in range(len(merged_clusters)):
        if not assigned_cids[i_cl]:
            while f"c{next_seq}" in used_cids:
                next_seq += 1
            assigned_cids[i_cl] = f"c{next_seq}"
            used_cids.add(assigned_cids[i_cl])
            next_seq += 1

    cluster_meta_list: list[tuple[str, list[_Occurrence], str | None, str | None, str | None, str | None]] = []
    for i_cl, cl in enumerate(merged_clusters):
        cid = assigned_cids[i_cl]
        for occ in cl:
            occ.person["cluster_id"] = cid
        sid_sources = [(o.native_sid, o.job.original_filename) for o in cl if o.native_sid]
        distinct_sids = {s for s, _ in sid_sources if s}
        cluster_sid: str | None = None
        cluster_sid_source: str | None = None
        if len(distinct_sids) == 1:
            cluster_sid, cluster_sid_source = sid_sources[0]
            for occ in cl:
                if occ.native_sid is None:
                    occ.person["student_id"] = cluster_sid
                    occ.person["cert"] = cluster_sid
                    occ.person["tier"] = "T1"
                    occ.person["student_id_source"] = cluster_sid_source
                    w_list = list(occ.person.get("warnings") or [])
                    if "STUDENT_ID_FROM_SIBLING" not in w_list:
                        w_list.append("STUDENT_ID_FROM_SIBLING")
                    occ.person["warnings"] = w_list
                    injected_student_ids += 1

        em_sources = [(o.native_email, o.job.original_filename) for o in cl if o.native_email]
        distinct_ems = {e for e, _ in em_sources if e}
        cluster_email: str | None = None
        cluster_email_source: str | None = None
        if len(distinct_ems) == 1:
            cluster_email, cluster_email_source = em_sources[0]
            for occ in cl:
                if occ.native_email is None:
                    occ.person["email"] = cluster_email
                    occ.person["email_source"] = cluster_email_source
                    w_list = list(occ.person.get("warnings") or [])
                    if "EMAIL_FROM_SIBLING" not in w_list:
                        w_list.append("EMAIL_FROM_SIBLING")
                    occ.person["warnings"] = w_list
                    injected_emails += 1

        cluster_meta_list.append(
            (cid, cl, cluster_sid, cluster_sid_source, cluster_email, cluster_email_source)
        )

    meta_by_norm_name: dict[
        str,
        list[tuple[str, list[_Occurrence], str | None, str | None, str | None, str | None]],
    ] = {}
    for meta in cluster_meta_list:
        meta_by_norm_name.setdefault(meta[1][0].norm_name, []).append(meta)
    for _n_name, metas_for_name in meta_by_norm_name.items():
        if len(metas_for_name) <= 1:
            continue
        for cid, cl, cluster_sid, _sid_src, cluster_email, _em_src in metas_for_name:
            if cluster_sid or cluster_email:
                is_ambiguous = False
            else:
                cl_affs = {o.norm_aff for o in cl if o.norm_aff}
                if len(cl_affs) != 1:
                    is_ambiguous = True
                else:
                    cl_aff = next(iter(cl_affs))
                    is_ambiguous = False
                    for (
                        other_cid,
                        other_cl,
                        other_sid,
                        _os_src,
                        other_em,
                        _oe_src,
                    ) in metas_for_name:
                        if other_cid == cid:
                            continue
                        other_affs = {o.norm_aff for o in other_cl if o.norm_aff}
                        if cl_aff in other_affs or (
                            not other_affs and (other_sid or other_em)
                        ):
                            is_ambiguous = True
                            break
            for occ in cl:
                occ.person["collision_ambiguous"] = is_ambiguous

    # Step 4: Run assign_people_identities on each job and coordinate cross-cluster logins
    for job in jobs:
        ppl_list = job_people_lists[job.id]
        for p in ppl_list:
            w_list = [str(w) for w in (p.get("warnings") or [])]
            if p.get("student_id") or (
                p.get("tier") == "T1" and "LOGIN_NAME_SUFFIXED" not in w_list
            ):
                cand = str(p.get("student_id") or p.get("login_name") or "").strip().lower()
                if cand and not _is_suffixed_pinyin_login(cand, str(p.get("name") or "")):
                    p["cert"] = cand
        assign_people_identities(
            ppl_list,
            existing_users=users,
            existing_people=people_db,
        )

    batch_used_logins: set[str] = set(RESERVED_LOGIN_NAMES) | {
        u.login_name.lower()
        for u in users
        if u.login_name is not None and u.login_name.strip()
    }
    for _cid, cl, cluster_sid, _sid_src, _cl_em, _em_src in cluster_meta_list:
        if cluster_sid:
            batch_used_logins.add(cluster_sid.lower())
        for occ in cl:
            rm = occ.person.get("match")
            if isinstance(rm, dict) and rm.get("status") == "existing":
                existing_login = str(occ.person.get("login_name") or "").strip().lower()
                if existing_login:
                    batch_used_logins.add(existing_login)

    for _cid, cl, cluster_sid, _sid_src, _cl_em, _em_src in cluster_meta_list:
        if cluster_sid:
            continue
        has_existing_or_conflict = any(
            isinstance(occ.person.get("match"), dict)
            and occ.person["match"].get("status") in ("existing", "conflict")
            for occ in cl
        )
        if has_existing_or_conflict:
            continue
        first_p = cl[0].person
        first_warnings = [str(w) for w in (first_p.get("warnings") or [])]
        if "LOGIN_NAME_REQUIRED" in first_warnings:
            continue
        base_cand = name_to_pinyin(cl[0].name) or str(first_p.get("login_name") or "").strip().lower()
        if not base_cand:
            continue
        if base_cand not in batch_used_logins:
            allocated = base_cand
            batch_used_logins.add(allocated)
            was_suffixed = False
        else:
            suffix = 2
            while f"{base_cand}{suffix}" in batch_used_logins:
                suffix += 1
            allocated = f"{base_cand}{suffix}"
            batch_used_logins.add(allocated)
            was_suffixed = True
        for occ in cl:
            occ.person["login_name"] = allocated
            occ_w = [
                str(w)
                for w in (occ.person.get("warnings") or [])
                if str(w) != "LOGIN_NAME_SUFFIXED"
            ]
            if was_suffixed and "LOGIN_NAME_SUFFIXED" not in occ_w:
                occ_w.append("LOGIN_NAME_SUFFIXED")
            occ.person["warnings"] = occ_w

    for job in jobs:
        job.preview = {**job_previews[job.id], "people": job_people_lists[job.id]}

    # Step 5: Build cluster descriptors and quality metrics
    clusters_out: list[dict[str, Any]] = []
    t1_covered = 0

    for (
        cluster_id,
        cl,
        cluster_sid,
        cluster_sid_source,
        cluster_email,
        cluster_email_source,
    ) in cluster_meta_list:
        first_occ = cl[0]
        members_out: list[dict[str, Any]] = []
        has_t1 = bool(cluster_sid)
        matched_uid_str: str | None = None
        resolved_login: str | None = None
        resolved_tier: str = "T1" if cluster_sid else ("T3" if cluster_email else "T2")
        resolved_aff: str | None = None

        for occ in cl:
            p = occ.person
            p_tier = str(p.get("tier") or "T2")
            p_login = str(p.get("login_name") or "")
            p_sid = str(p.get("student_id") or "").strip().lower() or None
            if p_tier == "T1" and p_sid and T1_CERT_RE.match(p_sid):
                has_t1 = True
                resolved_tier = "T1"
            if not resolved_login and p_login:
                resolved_login = p_login
            if not resolved_aff and occ.affiliation:
                resolved_aff = occ.affiliation

            raw_match = p.get("match")
            if isinstance(raw_match, dict) and raw_match.get("status") == "existing":
                uid_val = raw_match.get("user_id")
                if uid_val:
                    matched_uid_str = str(uid_val)
                    try:
                        matched_u = users_by_id.get(uuid.UUID(matched_uid_str))
                        if (
                            matched_u is not None
                            and matched_u.login_name
                            and T1_CERT_RE.match(matched_u.login_name.lower())
                            and not _is_suffixed_pinyin_login(
                                matched_u.login_name.lower(), matched_u.display_name
                            )
                        ):
                            has_t1 = True
                            resolved_tier = "T1"
                    except ValueError:
                        pass

            eff_as_of = _effective_job_as_of(occ.job)
            members_out.append(
                {
                    "job_id": str(occ.job_id),
                    "filename": occ.job.original_filename,
                    "as_of_time": eff_as_of.isoformat() if eff_as_of else None,
                    "key": occ.person_key,
                    "temp_id": occ.person_key,
                    "person_key": occ.person_key,
                    "name": occ.name,
                    "tier": p_tier,
                    "login_name": p_login,
                    "student_id": p_sid,
                    "student_id_source": p.get("student_id_source"),
                    "email": p.get("email"),
                    "email_source": p.get("email_source"),
                    "affiliation": occ.affiliation or None,
                    "warnings": list(p.get("warnings") or []),
                }
            )

        if has_t1:
            t1_covered += 1

        clusters_out.append(
            {
                "cluster_id": cluster_id,
                "name": first_occ.name,
                "tier": resolved_tier,
                "login_name": resolved_login,
                "student_id": cluster_sid,
                "student_id_source": cluster_sid_source,
                "email": cluster_email,
                "email_source": cluster_email_source,
                "affiliation": resolved_aff,
                "matched_user_id": matched_uid_str,
                "members": members_out,
            }
        )

    people_total = len(clusters_out)
    t1_ratio = round(t1_covered / people_total, 4) if people_total > 0 else 0.0

    # Group jobs by effective as_of_time
    jobs_by_as_of: dict[str, list[str]] = {}
    missing_as_of_jobs: list[str] = []
    for job in jobs:
        eff_date = _effective_job_as_of(job)
        if eff_date is None:
            missing_as_of_jobs.append(str(job.id))
        else:
            jobs_by_as_of.setdefault(eff_date.isoformat(), []).append(str(job.id))

    same_as_of_jobs = [
        group for group in jobs_by_as_of.values() if len(group) >= 2
    ]

    quality: dict[str, Any] = {
        "people_total": people_total,
        "t1_covered": t1_covered,
        "t1_ratio": t1_ratio,
        "injected_student_ids": injected_student_ids,
        "injected_emails": injected_emails,
        "name_collisions": name_collisions,
        "same_as_of_jobs": same_as_of_jobs,
        "missing_as_of_jobs": missing_as_of_jobs,
    }

    # Compute self_candidate if actor_user has no linked LabPerson
    self_candidate: dict[str, Any] | None = None
    if actor_user is not None and all(lp.user_id != actor_user.id for lp in people_db):
        actor_norm_name = norm(normalize_person_name(actor_user.display_name))
        if actor_norm_name and actor_norm_name not in collided_norm_names:
            matching_clusters = [
                c
                for c in clusters_out
                if norm(normalize_person_name(str(c.get("name") or ""))) == actor_norm_name
                and (
                    c.get("matched_user_id") is None
                    or str(c.get("matched_user_id")) == str(actor_user.id)
                )
            ]
            if len(matching_clusters) == 1:
                chosen = matching_clusters[0]
                self_candidate = {
                    "cluster_id": chosen["cluster_id"],
                    "name": chosen["name"],
                    "members": chosen["members"],
                }

    return {
        "clusters": clusters_out,
        "quality": quality,
        "self_candidate": self_candidate,
    }
