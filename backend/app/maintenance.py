"""Periodic data retention, purge-on-delete scrubbing, and maintenance tasks."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import redis.asyncio as redis
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.audit import run_retention, write_audit
from app.config import Settings
from app.db import get_sessionmaker
from app.logging_conf import get_logger
from app.models import Document, Message, RevokedToken

log = get_logger("maintenance")

LOCK_KEY = "maintenance:lock"
LOCK_TTL_SECONDS = 3600


async def purge_document(session: AsyncSession, document_id: uuid.UUID) -> int:
    """Hard-delete a document (cascading chunks & ACL) and scrub message citations."""
    doc_id_str = str(document_id)
    needle = json.dumps([{"document_id": doc_id_str}])
    rows = list(
        await session.scalars(
            select(Message)
            .where(text("citations @> CAST(:needle AS jsonb)"))
            .params(needle=needle)
        )
    )
    messages_scrubbed = 0
    for msg in rows:
        changed = False
        updated_citations: list[dict[str, object]] = []
        for item in msg.citations or []:
            if isinstance(item, dict) and str(item.get("document_id")) == doc_id_str:
                scrubbed_item = dict(item)
                scrubbed_item["excerpt"] = "[已删除]"
                scrubbed_item["title"] = "[已删除文档]"
                updated_citations.append(scrubbed_item)
                changed = True
            else:
                updated_citations.append(item)
        if changed:
            msg.citations = updated_citations
            flag_modified(msg, "citations")
            messages_scrubbed += 1

    await session.execute(delete(Document).where(Document.id == document_id))
    return messages_scrubbed


async def purge_expired_deleted_documents(
    session: AsyncSession, *, days: int
) -> tuple[int, int]:
    """Purge soft-deleted documents whose deleted_at is older than `days`."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    doc_ids = list(
        await session.scalars(
            select(Document.id).where(
                Document.deleted_at.is_not(None),
                Document.deleted_at < cutoff,
            )
        )
    )
    documents_purged = 0
    messages_scrubbed = 0
    for doc_id in doc_ids:
        messages_scrubbed += await purge_document(session, doc_id)
        documents_purged += 1
    return documents_purged, messages_scrubbed


async def delete_expired_messages(session: AsyncSession, *, days: int) -> int:
    """Delete messages older than `days`."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(
        delete(Message).where(Message.created_at < cutoff)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def delete_expired_revoked_tokens(session: AsyncSession) -> int:
    """Delete revoked JWT rows whose expiration timestamp has passed."""
    now = datetime.now(UTC)
    result = await session.execute(
        delete(RevokedToken).where(RevokedToken.expires_at < now)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def run_maintenance_once(
    settings: Settings,
    *,
    redis_client: Any | None = None,
) -> dict[str, int] | None:
    """Execute one full maintenance pass if the Redis distributed lock is acquired."""
    owns_client = redis_client is None
    client: Any = (
        redis.from_url(settings.redis_url, decode_responses=True)
        if redis_client is None
        else redis_client
    )
    try:
        acquired = await client.set(
            LOCK_KEY,
            uuid.uuid4().hex,
            nx=True,
            ex=LOCK_TTL_SECONDS,
        )
    finally:
        if owns_client:
            await client.aclose()

    if not acquired:
        return None

    maker = get_sessionmaker()
    async with maker() as session:
        audit_deleted = await run_retention(session, days=settings.audit_retention_days)
        documents_purged, messages_scrubbed = await purge_expired_deleted_documents(
            session, days=settings.deleted_document_retention_days
        )
        messages_deleted = await delete_expired_messages(
            session, days=settings.message_retention_days
        )
        revoked_tokens_deleted = await delete_expired_revoked_tokens(session)
        await session.commit()

    counts = {
        "audit_deleted": audit_deleted,
        "documents_purged": documents_purged,
        "messages_scrubbed": messages_scrubbed,
        "messages_deleted": messages_deleted,
        "revoked_tokens_deleted": revoked_tokens_deleted,
    }
    await write_audit(
        settings=settings,
        actor_id=None,
        action="maintenance_run",
        resource_type="system",
        detail=counts,
    )
    return counts


async def run_maintenance_worker(settings: Settings, stop: asyncio.Event) -> None:
    """Periodically run maintenance every MAINTENANCE_INTERVAL_SECONDS until stopped."""
    interval = max(float(settings.maintenance_interval_seconds), 1.0)
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except TimeoutError:
            pass
        try:
            await run_maintenance_once(settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("maintenance_worker_error", error=type(exc).__name__)
