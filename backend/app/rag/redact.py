"""PII redaction applied before any text is sent to an external model."""

from __future__ import annotations

import re

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
_LANDLINE = re.compile(
    r"(?<![A-Za-z\d.\-])0\d{2,3}-?\d{7,8}(?![A-Za-z\d\-]|\.[A-Za-z\d])"
)
_CN_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_CN_ID_BIRTH = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])$")
_BANK_CARD = re.compile(r"(?<![\d.])\d{16,19}(?!\d|\.\d)")
_PASSPORT = re.compile(r"(?<![A-Za-z\d\-])[EGDSP]\d{8}(?![A-Za-z\d])")
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def luhn_ok(digits: str) -> bool:
    if not (16 <= len(digits) <= 19) or not digits.isdigit():
        return False
    total = 0
    for idx, ch in enumerate(reversed(digits)):
        num = ord(ch) - 48
        if idx % 2 == 1:
            num *= 2
            if num > 9:
                num -= 9
        total += num
    return total % 10 == 0


def contains_email_or_phone(text: str) -> bool:
    if _EMAIL.search(text):
        return True
    scrubbed = _UUID.sub("", text)
    return bool(_PHONE.search(scrubbed) or _LANDLINE.search(scrubbed))


def redact_text(text: str) -> tuple[str, int]:
    count = 0

    def _sub(
        pattern: re.Pattern[str],
        label: str,
        value: str,
        predicate: object = None,
    ) -> str:
        nonlocal count

        def repl(match: re.Match[str]) -> str:
            nonlocal count
            raw = match.group(0)
            if callable(predicate) and not predicate(raw):
                return raw
            count += 1
            return label

        return pattern.sub(repl, value)

    def _is_definite_cn_id(raw: str) -> bool:
        return raw[-1] in ("X", "x") or _CN_ID_BIRTH.match(raw[6:14]) is not None

    cleaned = _sub(_EMAIL, "[REDACTED_EMAIL]", text)
    cleaned = _sub(_CN_ID, "[REDACTED_ID]", cleaned, _is_definite_cn_id)
    cleaned = _sub(_BANK_CARD, "[REDACTED_CARD]", cleaned, luhn_ok)
    cleaned = _sub(_CN_ID, "[REDACTED_ID]", cleaned)
    cleaned = _sub(_PHONE, "[REDACTED_PHONE]", cleaned)
    cleaned = _sub(_LANDLINE, "[REDACTED_PHONE]", cleaned)
    cleaned = _sub(_PASSPORT, "[REDACTED_PASSPORT]", cleaned)
    return cleaned, count
