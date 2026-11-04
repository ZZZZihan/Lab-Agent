"""Append-only audit log. Detail payloads cannot carry prompts or contact fields."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import get_sessionmaker
from app.models import AuditEvent

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


def sanitize_detail(detail: dict[str, Any] | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (detail or {}).items():
        if key.lower() in FORBIDDEN_KEYS:
            raise AuditRejected(f"audit field not allowed: {key}")
        if isinstance(value, str) and len(value) > 200:
            raise AuditRejected("audit string too long")
        if isinstance(value, list):
            if len(value) > 80:
                raise AuditRejected("audit list too long")
            for item in value:
                if not isinstance(item, (str, int, float)):
                    raise AuditRejected("audit list values must be scalars")
                if isinstance(item, str) and len(item) > 80:
                    raise AuditRejected("audit list item too long")
        elif isinstance(value, dict):
            raise AuditRejected("nested audit objects are not allowed")
        elif not isinstance(value, (str, int, float, bool)) and value is not None:
            raise AuditRejected("unsupported audit value")
        clean[key] = value
    return clean


def hash_ip(ip: str | None, secret: str) -> str | None:
    if not ip:
        return None
    digest = hmac.new(secret.encode(), ip.encode(), hashlib.sha256).hexdigest()
    return digest[:32]


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
    maker = get_sessionmaker()
    async with maker() as session:
        session.add(
            AuditEvent(
                actor_id=actor_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                request_id=request_id,
                ip_hash=hash_ip(ip, settings.jwt_secret),
                detail=payload,
            )
        )
        await session.commit()


async def run_retention(session: AsyncSession, *, days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    await session.execute(text("SELECT set_config('yanzheng.audit_retention', 'on', true)"))
    result = await session.execute(
        text("DELETE FROM audit_events WHERE created_at < :cutoff"),
        {"cutoff": cutoff},
    )
    return int(getattr(result, "rowcount", 0) or 0)
