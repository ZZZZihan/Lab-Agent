"""Deterministic SHA-256 entity fingerprints for people, achievements, projects, and facilities."""

from __future__ import annotations

import hashlib
import re
import unicodedata

FP_VERSION = "v1"


def norm(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").lower()
    return re.sub(r"[\W_]+", "", normalized)


def fp(*parts: str) -> str:
    return hashlib.sha256("|".join((FP_VERSION, *parts)).encode("utf-8")).hexdigest()


def person_fp(
    *,
    tier: str,
    login_name: str | None,
    name: str,
    affiliation: str | None = None,
    email: str | None = None,
) -> str:
    if tier == "T1" and login_name:
        return fp("person", "cert", norm(login_name))
    if email and email.strip():
        return fp("person", "email", norm(name), norm(affiliation), norm(email))
    return fp("person", "name", norm(name), norm(affiliation))


def achievement_fp(
    *,
    kind: str,
    title: str,
    year: int | None,
    raw_text: str,
    parse_quality: str = "structured",
) -> str:
    if kind == "patent" and (m := re.search(r"ZL\s?\d{8,13}(?:\.\d)?", raw_text, re.I)):
        return fp("ach", "patent", norm(m.group(0)))
    if kind == "software" and (m := re.search(r"\d{4}SR\d{5,8}", raw_text, re.I)):
        return fp("ach", "software", norm(m.group(0)))
    if parse_quality == "structured" and norm(title):
        return fp("ach", kind, norm(title)[:120], str(year or ""))
    return fp("ach", kind, "raw", norm(raw_text)[:200])


_REDACTED_TOKEN_RE = re.compile(r"(?:已脱敏|redacted)", re.IGNORECASE)


def is_redacted_placeholder(value: str | None) -> bool:
    if not value:
        return False
    return bool(_REDACTED_TOKEN_RE.search(value))


def project_fp(
    *,
    grant_no: str | None,
    title: str,
    funder: str | None,
    warnings: list[str] | None = None,
) -> str:
    if (
        grant_no
        and norm(grant_no)
        and not is_redacted_placeholder(grant_no)
        and "GRANT_NO_REDACTED" not in (warnings or [])
    ):
        return fp("proj", "grant", norm(grant_no))
    return fp("proj", "title", norm(title)[:120], norm(funder))


def facility_fp(
    *,
    category: str,
    name: str,
    spec: str | None = None,
) -> str:
    return fp("fac", norm(category), norm(name)[:120], norm(spec)[:80])
