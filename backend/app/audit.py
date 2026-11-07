"""Append-only audit log. Detail payloads cannot carry prompts or contact fields."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_sessionmaker
from app.models import AuditEvent
from app.rag.redact import contains_email_or_phone

GENESIS_PREV_HASH = "0" * 64
AUDIT_ADVISORY_LOCK_ID = 727274

FORBIDDEN_KEYS = {
    "prompt",
    "prompts",
    "query",
    "question",
    "content",
    "text",
    "email",
    "password",
    "token",
    "authorization",
    "excerpt",
    "messages",
    "body",
    "chunk",
    "answer",
}


class AuditRejected(ValueError):
    pass


def normalize_audit_value(value: Any) -> Any:
    """Normalize JSONB numeric representations (e.g. -0.0 -> 0.0) so hashes survive DB round-trip."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditRejected("audit float must be finite")
        if value == 0.0:
            return 0.0
        s = repr(value)
        if "e+" in s or "E+" in s:
            d = Decimal(s)
            if d == d.to_integral_value():
                return int(d)
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        return [normalize_audit_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): normalize_audit_value(v) for k, v in value.items()}
    return value


_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _is_uuid(value: str) -> bool:
    if len(value) != 36:
        return False
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False


def _is_sha256_hex(value: str) -> bool:
    return bool(_SHA256_HEX_RE.fullmatch(value))


def sanitize_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if key.lower() in FORBIDDEN_KEYS:
            raise AuditRejected(f"audit field not allowed: {key}")
        if isinstance(value, str):
            if len(value) > 200:
                raise AuditRejected("audit string too long")
            if (
                not _is_uuid(value)
                and not _is_sha256_hex(value)
                and contains_email_or_phone(value)
            ):
                raise AuditRejected("audit string value contains email or phone")
        if isinstance(value, list):
            if len(value) > 80:
                raise AuditRejected("audit list too long")
            for item in value:
                if not isinstance(item, (str, int, float)):
                    raise AuditRejected("audit list values must be scalars")
                if isinstance(item, str):
                    if len(item) > 80:
                        raise AuditRejected("audit list item too long")
                    if (
                        not _is_uuid(item)
                        and not _is_sha256_hex(item)
                        and contains_email_or_phone(item)
                    ):
                        raise AuditRejected("audit list item contains email or phone")
        elif isinstance(value, dict):
            raise AuditRejected("nested audit objects are not allowed")
        elif not isinstance(value, (str, int, float, bool)) and value is not None:
            raise AuditRejected("unsupported audit value")
        clean[key] = normalize_audit_value(value)
    return clean


def hash_ip(ip: str | None, secret: str) -> str | None:
    if not ip:
        return None
    digest = hmac.new(secret.encode(), ip.encode(), hashlib.sha256).hexdigest()
    return digest[:32]


def format_audit_timestamp(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def canonical_audit_json(
    *,
    created_at: datetime,
    actor_id: uuid.UUID | str | None,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    request_id: str | None,
    ip_hash: str | None,
    detail: dict[str, Any],
) -> str:
    payload = {
        "created_at": format_audit_timestamp(created_at),
        "actor_id": str(actor_id) if actor_id is not None else None,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "request_id": request_id,
        "ip_hash": ip_hash,
        "detail": normalize_audit_value(detail),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_row_hash(
    prev_hash: str,
    *,
    created_at: datetime,
    actor_id: uuid.UUID | str | None,
    action: str,
    resource_type: str | None,
    resource_id: str | None,
    request_id: str | None,
    ip_hash: str | None,
    detail: dict[str, Any],
) -> str:
    canonical = canonical_audit_json(
        created_at=created_at,
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        ip_hash=ip_hash,
        detail=detail,
    )
    return hashlib.sha256(f"{prev_hash}{canonical}".encode()).hexdigest()


async def write_audit(
    *,
    settings: Settings,
    actor_id: uuid.UUID | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    payload = sanitize_detail(detail)
    ip_hashed = hash_ip(ip, settings.resolved_audit_hmac_key)
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": AUDIT_ADVISORY_LOCK_ID},
        )
        last_hash = (
            await session.execute(
                text("SELECT row_hash FROM audit_events ORDER BY id DESC LIMIT 1")
            )
        ).scalar_one_or_none()
        prev_hash = str(last_hash).strip() if last_hash else GENESIS_PREV_HASH
        created_at = datetime.now(UTC)
        row_hash = compute_row_hash(
            prev_hash,
            created_at=created_at,
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=request_id,
            ip_hash=ip_hashed,
            detail=payload,
        )
        session.add(
            AuditEvent(
                created_at=created_at,
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                ip_hash=ip_hashed,
                detail=payload,
                prev_hash=prev_hash,
                row_hash=row_hash,
            )
        )
        await session.commit()


async def verify_audit_chain(session: AsyncSession) -> int | None:
    """Verify hash chain from the earliest non-null prev_hash. Returns first broken id or None."""
    first_id = await session.scalar(
        select(AuditEvent.id)
        .where(AuditEvent.prev_hash.is_not(None))
        .order_by(AuditEvent.id.asc())
        .limit(1)
    )
    if first_id is None:
        return None
    rows = list(
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.id >= first_id)
            .order_by(AuditEvent.id.asc())
        )
    )
    expected_prev_hash: str | None = None
    for idx, row in enumerate(rows):
        if row.prev_hash is None or row.row_hash is None or row.created_at is None:
            return int(row.id)
        curr_prev = row.prev_hash.strip()
        curr_row_hash = row.row_hash.strip()
        if idx > 0 and curr_prev != expected_prev_hash:
            return int(row.id)
        recomputed = compute_row_hash(
            curr_prev,
            created_at=row.created_at,
            actor_id=row.actor_id,
            action=row.action,
            resource_type=row.resource_type,
            resource_id=row.resource_id,
            request_id=row.request_id,
            ip_hash=row.ip_hash,
            detail=row.detail if isinstance(row.detail, dict) else {},
        )
        if recomputed != curr_row_hash:
            return int(row.id)
        expected_prev_hash = curr_row_hash
    return None


async def run_retention(session: AsyncSession, *, days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    await session.execute(text("SELECT set_config('labagent.audit_retention', 'on', true)"))
    result = await session.execute(
        text("DELETE FROM audit_events WHERE created_at < :cutoff"),
        {"cutoff": cutoff},
    )
    return int(getattr(result, "rowcount", 0) or 0)
