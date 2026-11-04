"""PII redaction applied before any text is sent to an external model."""

from __future__ import annotations

import re

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
_CN_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


def redact_text(text: str) -> tuple[str, int]:
    count = 0

    def _sub(pattern: re.Pattern[str], label: str, value: str) -> str:
        nonlocal count

        def repl(_match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            return label

        return pattern.sub(repl, value)

    cleaned = _sub(_EMAIL, "[REDACTED_EMAIL]", text)
    cleaned = _sub(_PHONE, "[REDACTED_PHONE]", cleaned)
    cleaned = _sub(_CN_ID, "[REDACTED_ID]", cleaned)
    return cleaned, count
