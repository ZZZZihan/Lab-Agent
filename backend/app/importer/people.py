"""3-tier adaptive login_name generation (T1 student/staff ID, T2 pinyin, T3 pinyin + email)."""

from __future__ import annotations

import re
import unicodedata
import uuid

from pypinyin import Style, lazy_pinyin

from app.importer.fingerprint import norm, person_fp
from app.importer.pii import is_pii_id_or_phone
from app.models import LabPerson, User

T1_CERT_RE = re.compile(r"^(?=.*\d)[a-z0-9]{6,12}$")
LOGIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

RESERVED_LOGIN_NAMES: frozenset[str] = frozenset(
    {"admin", "root", "system", "api", "null", "test", "labagent"}
)

SURNAME_PINYIN_OVERRIDES: dict[str, str] = {
    "尉迟": "yuchi",
    "万俟": "moqi",
    "澹台": "tantai",
    "皇甫": "huangfu",
    "诸葛": "zhuge",
    "东方": "dongfang",
    "上官": "shangguan",
    "欧阳": "ouyang",
    "司马": "sima",
    "司徒": "situ",
    "司空": "sikong",
    "令狐": "linghu",
    "长孙": "zhangsun",
    "单": "shan",
    "曾": "zeng",
    "解": "xie",
    "仇": "qiu",
    "区": "ou",
    "朴": "piao",
    "查": "zha",
    "盖": "ge",
    "缪": "miao",
    "任": "ren",
    "华": "hua",
    "纪": "ji",
}


def normalize_person_name(raw_name: str) -> str:
    cleaned = unicodedata.normalize("NFKC", raw_name or "")
    cleaned = re.sub(r"[​-‏﻿\s]+", "", cleaned)
    return cleaned


def clean_email(raw_email: str | None) -> str | None:
    if not raw_email:
        return None
    normalized = unicodedata.normalize("NFKC", raw_email).strip().lower()
    if not normalized or not EMAIL_RE.match(normalized):
        return None
    return normalized


def name_to_pinyin(raw_name: str) -> str:
    name = normalize_person_name(raw_name).replace("·", "")
    if not name:
        return ""
    prefix = ""
    rest = name
    if len(name) >= 2 and name[:2] in SURNAME_PINYIN_OVERRIDES:
        prefix = SURNAME_PINYIN_OVERRIDES[name[:2]]
        rest = name[2:]
    elif name[0] in SURNAME_PINYIN_OVERRIDES:
        prefix = SURNAME_PINYIN_OVERRIDES[name[0]]
        rest = name[1:]
    rest_syllables = lazy_pinyin(rest, style=Style.NORMAL, errors="ignore") if rest else []
    combined = (prefix + "".join(rest_syllables)).lower()
    return re.sub(r"[^a-z0-9]", "", combined)


def decide_login_identity(
    *,
    name: str,
    cert: str | None,
    email: str | None,
) -> tuple[str, str, str | None, list[str]]:
    """Return `(tier, base_login_name, normalized_email, warnings)`."""
    warnings: list[str] = []
    valid_email = clean_email(email)
    raw_cert = unicodedata.normalize("NFKC", cert or "").strip()
    if raw_cert:
        if is_pii_id_or_phone(raw_cert):
            warnings.append("CERT_DROPPED")
            raw_cert = ""
        else:
            lowered_cert = raw_cert.lower()
            if T1_CERT_RE.match(lowered_cert):
                return "T1", lowered_cert, valid_email, warnings
    base = name_to_pinyin(name)
    tier = "T3" if valid_email else "T2"
    if len(base) < 3:
        warnings.append("LOGIN_NAME_REQUIRED")
    return tier, base, valid_email, warnings


def is_valid_login_name(login_name: str) -> bool:
    if not login_name or login_name in RESERVED_LOGIN_NAMES:
        return False
    return bool(LOGIN_NAME_RE.match(login_name))


def assign_people_identities(
    people: list[dict[str, object]],
    existing_users: list[User] | None = None,
    existing_people: list[LabPerson] | None = None,
) -> None:
    """Populate `tier`, `login_name`, `email`, `fingerprint`, `match`, `warnings` in-place."""
    users = existing_users or []
    users_by_id: dict[uuid.UUID, User] = {u.id: u for u in users}
    by_email: dict[str, User] = {
        u.email.lower(): u for u in users if u.email is not None and u.email.strip()
    }
    by_login: dict[str, User] = {
        u.login_name.lower(): u
        for u in users
        if u.login_name is not None and u.login_name.strip()
    }
    all_existing_people = list(existing_people or [])
    by_fp: dict[str, User] = {}
    lp_by_fp: dict[str, LabPerson] = {}
    lp_with_user: list[LabPerson] = []
    for lp in all_existing_people:
        if lp.user_id is not None and lp.user_id in users_by_id:
            lp_with_user.append(lp)
            if lp.entity_fingerprint:
                by_fp[lp.entity_fingerprint] = users_by_id[lp.user_id]
                lp_by_fp[lp.entity_fingerprint] = lp

    def _lp_has_t1(lp: LabPerson) -> bool:
        if lp.entity_fingerprint:
            non_t1_fp = person_fp(
                tier="T3" if lp.email else "T2",
                login_name=None,
                name=lp.name,
                affiliation=lp.affiliation,
                email=lp.email,
            )
            return lp.entity_fingerprint != non_t1_fp
        u = users_by_id.get(lp.user_id) if lp.user_id else None
        login_cand = str(
            (u.login_name if u and u.login_name else None)
            or getattr(lp, "login_name", None)
            or ""
        ).strip().lower()
        return bool(login_cand and T1_CERT_RE.match(login_cand))

    incoming_name_counts: dict[str, int] = {}
    incoming_name_aff_counts: dict[tuple[str, str], int] = {}
    for p in people:
        if (
            p.get("exclude") is True
            or p.get("excluded") is True
            or p.get("include") is False
            or p.get("imported") is False
        ):
            continue
        pn = norm(normalize_person_name(str(p.get("name") or "")))
        pa = norm(str(p.get("affiliation") or "").strip())
        if pn:
            incoming_name_counts[pn] = incoming_name_counts.get(pn, 0) + 1
            incoming_name_aff_counts[(pn, pa)] = (
                incoming_name_aff_counts.get((pn, pa), 0) + 1
            )

    used_logins: set[str] = set(RESERVED_LOGIN_NAMES) | set(by_login.keys())
    used_matched_user_ids: set[uuid.UUID] = set()

    def _can_fallback_to_lp(
        lp: LabPerson,
        *,
        inc_has_t1: bool,
        inc_email: str | None,
    ) -> bool:
        if (
            lp.user_id is None
            or lp.user_id not in users_by_id
            or lp.user_id in used_matched_user_ids
        ):
            return False
        if inc_has_t1 and _lp_has_t1(lp):
            return False
        u = users_by_id.get(lp.user_id)
        lp_em = clean_email(lp.email or (u.email if u else None))
        return not (inc_email and lp_em and lp_em != inc_email)

    for idx, person in enumerate(people, start=1):
        person.setdefault("key", str(person.get("temp_id") or f"p{idx}"))
        person["temp_id"] = str(person["key"])
        person["role"] = "pi" if person.get("project_role") == "applicant" else "member"
        name = normalize_person_name(str(person.get("name") or ""))
        person["name"] = name
        popped_cert = str(person.pop("cert", "") or "").strip()
        saved_student_id = str(person.pop("student_id", "") or "").strip()
        if (
            saved_student_id
            and T1_CERT_RE.match(saved_student_id.lower())
            and (not popped_cert or not T1_CERT_RE.match(popped_cert.lower()))
        ):
            raw_cert = saved_student_id
        else:
            raw_cert = popped_cert or saved_student_id
        raw_email = str(person.get("email") or "") if person.get("email") else None

        tier, base_login, valid_email, item_warnings = decide_login_identity(
            name=name,
            cert=raw_cert,
            email=raw_email,
        )
        if tier == "T1" and base_login:
            person["student_id"] = base_login
        existing_warnings = [
            w
            for w in list(person.get("warnings") or [])  # type: ignore[call-overload]
            if w not in ("LOGIN_CONFLICT", "LOGIN_NAME_REQUIRED", "LOGIN_NAME_SUFFIXED")
        ]
        for w in item_warnings:
            if w not in existing_warnings:
                existing_warnings.append(w)

        person["tier"] = tier
        person["email"] = valid_email
        affiliation = str(person.get("affiliation") or "").strip() or None
        candidate_fp = person_fp(
            tier=tier,
            login_name=base_login if tier == "T1" else None,
            name=name,
            affiliation=affiliation,
            email=valid_email,
        )

        matched_user: User | None = None
        matched_lp: LabPerson | None = None
        match_status = "new"
        if valid_email and valid_email in by_email:
            matched_user = by_email[valid_email]
            match_status = "existing"
            matched_lp = next(
                (lp for lp in lp_with_user if lp.user_id == matched_user.id),
                None,
            )
        elif (
            candidate_fp in by_fp
            and by_fp[candidate_fp].id not in used_matched_user_ids
            and not (
                valid_email
                and (by_fp[candidate_fp].email or "").strip()
                and (by_fp[candidate_fp].email or "").strip().lower() != valid_email.lower()
            )
        ):
            matched_user = by_fp[candidate_fp]
            match_status = "existing"
            matched_lp = lp_by_fp.get(candidate_fp)
        elif tier == "T1" and base_login in by_login:
            candidate = by_login[base_login]
            if normalize_person_name(candidate.display_name) == name:
                matched_user = candidate
                match_status = "existing"
                matched_lp = next(
                    (lp for lp in lp_with_user if lp.user_id == candidate.id),
                    None,
                )
            else:
                matched_user = candidate
                match_status = "conflict"
                if "LOGIN_CONFLICT" not in existing_warnings:
                    existing_warnings.append("LOGIN_CONFLICT")
        else:
            norm_name = norm(name)
            norm_aff = norm(affiliation)
            inc_has_t1 = tier == "T1" and bool(base_login)

            if norm_name and incoming_name_aff_counts.get((norm_name, norm_aff), 0) == 1:
                same_name_aff = [
                    lp
                    for lp in all_existing_people
                    if norm(normalize_person_name(lp.name)) == norm_name
                    and norm(lp.affiliation) == norm_aff
                ]
                compat_aff = [
                    lp
                    for lp in same_name_aff
                    if _can_fallback_to_lp(
                        lp,
                        inc_has_t1=inc_has_t1,
                        inc_email=valid_email,
                    )
                ]
                if len(compat_aff) == 1 and len(same_name_aff) == 1:
                    matched_lp = compat_aff[0]
                    matched_user = users_by_id[matched_lp.user_id]  # type: ignore[index]
                    match_status = "existing"
                elif not same_name_aff and incoming_name_counts.get(norm_name, 0) == 1:
                    same_name_only = [
                        lp
                        for lp in all_existing_people
                        if norm(normalize_person_name(lp.name)) == norm_name
                    ]
                    compat_name = [
                        lp
                        for lp in same_name_only
                        if _can_fallback_to_lp(
                            lp,
                            inc_has_t1=inc_has_t1,
                            inc_email=valid_email,
                        )
                        and (not norm_aff or not norm(lp.affiliation))
                    ]
                    if len(compat_name) == 1 and len(same_name_only) == 1:
                        matched_lp = compat_name[0]
                        matched_user = users_by_id[matched_lp.user_id]  # type: ignore[index]
                        match_status = "existing"

        if match_status == "existing" and matched_user is not None:
            used_matched_user_ids.add(matched_user.id)
            if matched_user.login_name:
                allocated = matched_user.login_name
                used_logins.add(allocated.lower())
            else:
                allocated = _allocate_unique_login(base_login, used_logins, existing_warnings)
            person["login_name"] = allocated
            person["match"] = {"status": "existing", "user_id": str(matched_user.id)}
        elif match_status == "conflict" and matched_user is not None:
            person["login_name"] = base_login
            person["match"] = {"status": "conflict", "user_id": str(matched_user.id)}
        else:
            if not base_login or "LOGIN_NAME_REQUIRED" in existing_warnings:
                person["login_name"] = base_login
            elif tier == "T1":
                person["login_name"] = base_login
                used_logins.add(base_login)
            else:
                allocated = _allocate_unique_login(base_login, used_logins, existing_warnings)
                person["login_name"] = allocated
            person["match"] = {"status": "new"}

        person["warnings"] = existing_warnings
        if (
            match_status == "existing"
            and matched_lp is not None
            and matched_lp.entity_fingerprint
            and not (tier == "T1" and base_login and not _lp_has_t1(matched_lp))
        ):
            person["fingerprint"] = matched_lp.entity_fingerprint
        else:
            person["fingerprint"] = person_fp(
                tier=tier,
                login_name=(
                    base_login
                    if (tier == "T1" and base_login)
                    else str(person.get("login_name") or "")
                ),
                name=name,
                affiliation=affiliation,
                email=valid_email,
            )


def _allocate_unique_login(
    base: str,
    used_logins: set[str],
    warnings: list[str],
) -> str:
    if not base:
        return ""
    if base not in used_logins:
        used_logins.add(base)
        return base
    suffix = 2
    while f"{base}{suffix}" in used_logins:
        suffix += 1
    candidate = f"{base}{suffix}"
    used_logins.add(candidate)
    if "LOGIN_NAME_SUFFIXED" not in warnings:
        warnings.append("LOGIN_NAME_SUFFIXED")
    return candidate
