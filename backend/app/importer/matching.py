"""Deterministic person name normalization, splitting, SQL ILIKE escaping, and dossier attribution (Spec v2.2 §4)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.models import LabAchievement, LabProject

ZERO_WIDTH_AND_SPACE_RE = re.compile(r"[\s​-‏﻿　]+")
MIDDLE_DOT_RE = re.compile(r"[·•．⋅∙]")
TRAILING_PAREN_RE = re.compile(r"[\s*#†‡]*[（(\[【][^）)\]】]*[）)\]】][\s*#†‡]*$")
TRAILING_MARK_RE = re.compile(r"[\s*#†‡¹²³⁴⁵⁶⁷⁸⁹⁰]+$")
TRAILING_ROLE_WORD_RE = re.compile(
    r"(?:主持人|负责人|通讯作者|第一作者|共同第一作者|指导教师|导师|主持|参与人|主要参与人|参与|参加)$"
)
EXPLICIT_DELIM_RE = re.compile(r"[、，,;；/／\n\r\t]+")
PARTICIPANT_CLAUSE_RE = re.compile(
    r"(?:主要参与人|参与人员|参与成员|课题组成员|项目组成员|参与人|参加人|成员)\s*[:：]\s*([^。；;\n]+)"
)
PARTICIPANT_INLINE_RE = re.compile(
    r"([一-龥·•．\s、，,]{2,40}?)\s*[（(]?(?:参与|参加)[)）]?(?=[，,。；;\s]|$)"
)

NOISE_TOKENS: frozenset[str] = frozenset(
    {
        "等",
        "etal",
        "et",
        "al",
        "课题组",
        "项目组",
        "研究组",
        "实验室",
        "全员",
        "全体",
        "无",
        "none",
        "null",
    }
)


def normalize_person_name(raw: str | None) -> str:
    """Normalize a person name for deterministic attribution (Spec v2.2 §4.2).

    1. Unicode NFKC
    2. Strip leading/trailing whitespace
    3. Strip trailing parentheticals or asterisk markers (e.g. '郭建民（负责人）', '万振凯*', '徐国伟(通讯作者)')
    4. Unify middle dots (· / • / ． -> ·)
    5. Remove all whitespace and zero-width characters (handles '万 振 凯', '郭　建民')
    """
    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if not text:
        return ""
    # Repeatedly strip trailing parenthetical annotations and markers
    prev = None
    while prev != text:
        prev = text
        text = TRAILING_PAREN_RE.sub("", text).strip()
        text = TRAILING_MARK_RE.sub("", text).strip()

    text = MIDDLE_DOT_RE.sub("·", text)
    text = ZERO_WIDTH_AND_SPACE_RE.sub("", text)

    # Strip any remaining parentheticals after whitespace removal
    prev = None
    while prev != text:
        prev = text
        text = TRAILING_PAREN_RE.sub("", text)
        text = TRAILING_MARK_RE.sub("", text)

    return text


def split_names(raw: str | None) -> list[str]:
    """Split an author or participant string into normalized person names (Spec v2.2 §4.2)."""
    if not raw:
        return []
    normalized = unicodedata.normalize("NFKC", str(raw)).strip()
    if not normalized:
        return []

    segments = EXPLICIT_DELIM_RE.split(normalized)
    tokens: list[str] = []

    for seg in segments:
        s = seg.strip()
        if not s:
            continue
        # Remove trailing parentheticals before checking internal spaces
        prev = None
        while prev != s:
            prev = s
            s = TRAILING_PAREN_RE.sub("", s).strip()
            s = TRAILING_MARK_RE.sub("", s).strip()
        if not s:
            continue

        # Check if segment contains space-separated Chinese names vs a single spaced Chinese name
        parts = [p for p in re.split(r"[\s　]+", s) if p]
        if len(parts) > 1 and all(re.match(r"^[一-龥·•．*#†‡（()）]+$", p) for p in parts):
            cleaned_parts = [normalize_person_name(p) for p in parts]
            # If every part has length >= 2, treat space as separator between multiple Chinese names
            if all(len(cp) >= 2 for cp in cleaned_parts if cp):
                for cp in cleaned_parts:
                    if _is_valid_name_token(cp):
                        tokens.append(cp)
                continue

        norm_tok = normalize_person_name(s)
        if _is_valid_name_token(norm_tok):
            tokens.append(norm_tok)

    return tokens


def _is_valid_name_token(tok: str) -> bool:
    if not tok:
        return False
    lowered = tok.lower()
    if lowered in NOISE_TOKENS:
        return False
    if tok.isdigit():
        return False
    return True


def build_ilike_pattern(raw_name: str) -> str:
    """Build a safe SQL ILIKE pattern with `%`, `_`, `\\` escaped.

    Uses character-interleaved `%` for 2..6 char CJK names so spaced variants like `'万 振 凯'`
    are also recalled by PostgreSQL before exact Python token verification.
    """
    norm_name = normalize_person_name(raw_name)
    source = norm_name or (raw_name or "").strip()
    if not source:
        return "%"

    def _esc(ch: str) -> str:
        if ch in ("\\", "%", "_"):
            return f"\\{ch}"
        return ch

    if 2 <= len(source) <= 64:
        inner = "%".join(_esc(c) for c in source)
        return f"%{inner}%"
    escaped = "".join(_esc(c) for c in source)
    return f"%{escaped}%"


def _extract_project_participant_tokens(project: LabProject | Any) -> list[str]:
    """Extract normalized participant names from a `LabProject`."""
    tokens: list[str] = []

    # 1. Explicit participants attribute if present
    raw_participants = getattr(project, "participants", None)
    if isinstance(raw_participants, str) and raw_participants.strip():
        tokens.extend(split_names(raw_participants))
    elif isinstance(raw_participants, (list, tuple)):
        for item in raw_participants:
            norm_item = normalize_person_name(str(item))
            if _is_valid_name_token(norm_item):
                tokens.append(norm_item)

    # 2. If pi_name contains multiple names separated by delimiters
    pi_raw = getattr(project, "pi_name", None)
    if isinstance(pi_raw, str) and pi_raw.strip():
        pi_list = split_names(pi_raw)
        if len(pi_list) > 1:
            tokens.extend(pi_list[1:])

    # 3. Extract from raw_text and relationship_note
    for text_field in (
        getattr(project, "raw_text", None),
        getattr(project, "relationship_note", None),
    ):
        if not isinstance(text_field, str) or not text_field.strip():
            continue
        for m in PARTICIPANT_CLAUSE_RE.finditer(text_field):
            tokens.extend(split_names(m.group(1)))
        for m in PARTICIPANT_INLINE_RE.finditer(text_field):
            for tok in split_names(m.group(1)):
                stripped = TRAILING_ROLE_WORD_RE.sub("", tok)
                if _is_valid_name_token(stripped):
                    tokens.append(stripped)

    return tokens


def classify_project_for_person(
    project: LabProject | Any, target_norm_name: str
) -> str | None:
    """Return `'pi'`, `'participant'`, or `None` for `project` with respect to `target_norm_name` (Spec v2.2 §4.3)."""
    if not target_norm_name:
        return None

    pi_raw = getattr(project, "pi_name", None)
    lab_role = str(getattr(project, "lab_role", "host") or "host")
    raw_text = str(getattr(project, "raw_text", "") or "")

    pi_tokens = split_names(pi_raw) if isinstance(pi_raw, str) else []
    pi_primary = pi_tokens[0] if pi_tokens else normalize_person_name(pi_raw)

    explicitly_pi_in_raw = bool(
        re.search(
            rf"{re.escape(target_norm_name)}\s*(?:[（(]?(?:主持|负责人|申请人)|[，,、]?\s*主持)",
            ZERO_WIDTH_AND_SPACE_RE.sub("", raw_text),
        )
    )
    explicitly_participant_only = bool(
        lab_role == "participant"
        and pi_primary == target_norm_name
        and not explicitly_pi_in_raw
    )

    if pi_primary == target_norm_name and not explicitly_participant_only:
        return "pi"

    participant_tokens = _extract_project_participant_tokens(project)
    if explicitly_participant_only or target_norm_name in participant_tokens:
        return "participant"

    return None


def classify_achievement_for_person(
    ach: LabAchievement | Any, target_norm_name: str
) -> tuple[str, int] | None:
    """Return `(relation_role, author_position)` or `None` for `ach` (Spec v2.2 §4.4)."""
    if not target_norm_name:
        return None

    authors_text = getattr(ach, "authors_text", None)
    first_author_raw = getattr(ach, "first_author", None)

    author_tokens = split_names(authors_text)
    first_norm = normalize_person_name(first_author_raw)

    # Also check leading author prefix in raw_text if authors_text and first_author are empty
    if not author_tokens and not first_norm:
        raw_text = str(getattr(ach, "raw_text", "") or "").strip()
        m_lead = re.match(r"^([^:：《\n]{2,30})\s*[:：]", raw_text)
        if m_lead:
            author_tokens = split_names(m_lead.group(1))

    if first_norm and first_norm not in author_tokens:
        author_tokens = [first_norm, *author_tokens]

    first = first_norm or (author_tokens[0] if author_tokens else "")

    if first and target_norm_name == first:
        return "first_author", 1

    if target_norm_name in author_tokens:
        pos = author_tokens.index(target_norm_name) + 1
        if pos == 1:
            return "first_author", 1
        return "coauthor", pos

    return None
