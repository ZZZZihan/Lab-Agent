"""Async background worker for document chunking and embedding."""

from __future__ import annotations

import asyncio
import time
import uuid

from sqlalchemy import delete, text

from app.audit import scrub_audit_str, write_audit
from app.config import Settings
from app.db import get_sessionmaker
from app.logging_conf import get_logger
from app.models import Chunk, Document
from app.rag.embedder import Embedder, EmbeddingError
from app.rag.ingest import index_document

log = get_logger("ingest_worker")


def _extract_error_code(exc: BaseException) -> str:
    if isinstance(exc, EmbeddingError):
        raw = str(exc.code or type(exc).__name__)
    else:
        code_attr = getattr(exc, "code", None)
        raw = str(code_attr) if isinstance(code_attr, str) and code_attr else type(exc).__name__
    return raw[:64]


async def recover_processing() -> int:
    """Reset documents stuck in 'processing' back to 'pending' on startup."""
    maker = get_sessionmaker()
    async with maker() as session:
        result = await session.execute(
            text(
                """
                UPDATE documents
                   SET status = 'pending',
                       updated_at = now()
                 WHERE status = 'processing'
                   AND deleted_at IS NULL
                """
            )
        )
        await session.commit()
        return int(getattr(result, "rowcount", 0) or 0)


async def process_next_pending(embedder: Embedder, settings: Settings) -> uuid.UUID | None:
    """Claim and index the oldest pending document. Returns the document id if one was claimed."""
    maker = get_sessionmaker()
    doc_id: uuid.UUID | None = None
    attempts = 0

    async with maker() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id, attempts
                      FROM documents
                     WHERE status = 'pending'
                       AND deleted_at IS NULL
                     ORDER BY created_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                    """
                )
            )
        ).first()
        if row is None:
            return None
        raw_id = row[0]
        doc_id = raw_id if isinstance(raw_id, uuid.UUID) else uuid.UUID(str(raw_id))
        attempts = int(row[1] or 0) + 1
        await session.execute(
            text(
                """
                UPDATE documents
                   SET status = 'processing',
                       attempts = :attempts,
                       updated_at = now()
                 WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": str(doc_id), "attempts": attempts},
        )
        await session.commit()

    started = time.perf_counter()
    try:
        async with maker() as session:
            doc = await session.get(Document, doc_id)
            if doc is None or doc.deleted_at is not None:
                return doc_id
            await session.execute(delete(Chunk).where(Chunk.document_id == doc.id))
            chunk_count = await index_document(
                session,
                embedder=embedder,
                document=doc,
                storage=settings.embedding_storage,
            )
            doc.status = "ready"
            doc.error_code = None
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            await write_audit(
                settings=settings,
                actor_id=None,
                action="document_indexed",
                resource_type="document",
                resource_id=str(doc_id),
                detail={"chunks": chunk_count, "ms": elapsed_ms},
                session=session,
            )
            await session.commit()
    except Exception as exc:
        error_code = _extract_error_code(exc)
        next_status = "pending" if attempts < settings.ingest_max_attempts else "failed"
        log.warning(
            "document_index_failed",
            document_id=str(doc_id),
            attempts=attempts,
            status=next_status,
            error_code=error_code,
        )
        async with maker() as session:
            await session.execute(
                text(
                    """
                    UPDATE documents
                       SET status = :status,
                           error_code = :error_code,
                           updated_at = now()
                     WHERE id = CAST(:id AS uuid)
                    """
                ),
                {
                    "id": str(doc_id),
                    "status": next_status,
                    "error_code": error_code,
                },
            )
            await write_audit(
                settings=settings,
                actor_id=None,
                action="document_index_failed",
                resource_type="document",
                resource_id=str(doc_id),
                detail={
                    "error": scrub_audit_str(error_code, max_len=64),
                    "attempts": attempts,
                },
                session=session,
            )
            await session.commit()

    return doc_id


async def run_worker(embedder: Embedder, settings: Settings, stop: asyncio.Event) -> None:
    """Continuously poll for pending documents until stop is set."""
    poll_seconds = max(float(settings.ingest_poll_seconds), 0.05)
    while not stop.is_set():
        try:
            processed = await process_next_pending(embedder, settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("ingest_worker_loop_error", error=type(exc).__name__)
            processed = None
        if processed is not None:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        except TimeoutError:
            continue
