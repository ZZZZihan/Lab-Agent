"""PII detection, physical dropping, free-text scrubbing, and zero-leak assertions."""

from __future__ import annotations

import json
import re

# 18-digit ID card (regardless of checksum — err on the side of safety)
ID18_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?![0-9Xx])")
# 15-digit legacy ID card
ID15_RE = re.compile(r"(?<!\d)\d{15}(?!\d)")
# 11-digit mainland mobile phone
MOBILE_RE = re.compile(
    r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}(?!\d)"
)
# Landline with area code hyphen (e.g. 022-83951234)
TEL_RE = re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)")

_UUID_RE = re.compile(
    r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?![0-9A-Za-z])"
)
_HEX64_RE = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{64}(?![0-9A-Za-z])")

PII_REPLACEMENT = "[已脱敏]"
_CSV_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "|", "\t", "\r", "\n")
_CSV_LEADING_CTRL_RE = re.compile(r"^[\x00-\x20]+")


def escape_csv_cell(value: object) -> str:
    """Neutralize spreadsheet formula/DDE injection prefixes in exported CSV cells."""
    if value is None:
        return ""
    text_val = str(value)
    if not text_val:
        return ""
    stripped = _CSV_LEADING_CTRL_RE.sub("", text_val)
    if text_val.startswith(_CSV_DANGEROUS_PREFIXES) or stripped.startswith(
        _CSV_DANGEROUS_PREFIXES
    ):
        return f"'{text_val}"
    return text_val


class PIILeakError(RuntimeError):
    """Raised when unredacted ID card or phone number is detected in output."""


def is_pii_id_or_phone(value: str | None) -> bool:
    """Return True if value matches an ID card or phone number pattern."""
    if not value:
        return False
    cleaned = _HEX64_RE.sub("", _UUID_RE.sub("", value.strip()))
    if not cleaned:
        return False
    compact = re.sub(r"[\s-]+", "", cleaned)
    return bool(
        ID18_RE.search(cleaned)
        or ID18_RE.search(compact)
        or ID15_RE.search(cleaned)
        or ID15_RE.search(compact)
        or MOBILE_RE.search(cleaned)
        or MOBILE_RE.search(compact)
        or TEL_RE.search(cleaned)
    )


def scrub_free_text(text: str | None) -> tuple[str, int]:
    """Replace ID cards and phone numbers in free text with `[已脱敏]`.

    Returns `(scrubbed_text, redaction_count)`.
    """
    if not text:
        return "", 0
    preserved: list[tuple[str, str]] = []

    def _stash(match: re.Match[str]) -> str:
        idx = len(preserved)
        alpha_id = "".join(chr(ord("A") + int(d)) for d in str(idx))
        token = f"\x00__SAFE_{alpha_id}__\x00"
        preserved.append((token, match.group(0)))
        return token

    out = _HEX64_RE.sub(_stash, _UUID_RE.sub(_stash, text))
    total = 0
    for pattern in (ID18_RE, ID15_RE, MOBILE_RE, TEL_RE):
        out, count = pattern.subn(PII_REPLACEMENT, out)
        total += count
    for token, orig in preserved:
        out = out.replace(token, orig)
    return out, total


def assert_no_pii(payload: str | dict[str, object] | list[object]) -> None:
    """Verify zero 18/15-digit ID card numbers or 11-digit mobile numbers remain."""
    if isinstance(payload, str):
        serialized = payload
    elif isinstance(payload, dict):
        cleaned = {k: v for k, v in payload.items() if not str(k).startswith("_")}
        serialized = json.dumps(cleaned, ensure_ascii=False, default=str)
    else:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
    sanitized = _HEX64_RE.sub("", _UUID_RE.sub("", serialized))
    for name, pattern in (
        ("id18", ID18_RE),
        ("id15", ID15_RE),
        ("mobile", MOBILE_RE),
        ("tel", TEL_RE),
    ):
        if pattern.search(sanitized):
            raise PIILeakError(f"Unredacted PII pattern ({name}) detected in payload")
