"""Embedding index metadata inspection and consistency checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

IndexState = Literal["ok", "missing_meta", "mismatch"]

HASHING_DEFAULT_FINGERPRINT = (
    "hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442"
)

_COL_TYPE_RE = re.compile(r"^(vector|halfvec)\((\d+)\)$")


@dataclass(frozen=True)
class IndexMeta:
    fingerprint: str
    embedder: str
    model: str
    dim: int
    storage: str
    index_kind: str
    updated_at: datetime | None = None

    @property
    def slug(self) -> str:
        return re.sub(r"[^a-z0-9]", "-", self.fingerprint.lower())


async def column_type(session: AsyncSession) -> tuple[str, int]:
    """Parse PostgreSQL format_type(atttypid, atttypmod) for chunks.embedding."""
    row = await session.execute(
        text(
            """
            SELECT format_type(a.atttypid, a.atttypmod) AS fmt
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = 'chunks'
              AND a.attname = 'embedding'
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND n.nspname = current_schema()
            """
        )
    )
    fmt = row.scalar_one_or_none()
    if not isinstance(fmt, str):
        raise RuntimeError("chunks.embedding column not found")
    match = _COL_TYPE_RE.match(fmt.strip())
    if match is None:
        raise RuntimeError(f"Unsupported chunks.embedding type format: {fmt!r}")
    return match.group(1), int(match.group(2))


async def has_hnsw_index(session: AsyncSession) -> bool:
    """Return True if chunks_embedding_hnsw exists in the current schema."""
    row = await session.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_indexes
                WHERE schemaname = current_schema()
                  AND tablename = 'chunks'
                  AND indexname = 'chunks_embedding_hnsw'
            )
            """
        )
    )
    return bool(row.scalar_one())


async def read_meta(session: AsyncSession) -> IndexMeta | None:
    """Read the single row from embedding_index_meta if present."""
    row = await session.execute(
        text(
            """
            SELECT fingerprint, embedder, model, dim, storage, index_kind, updated_at
            FROM embedding_index_meta
            WHERE id = 1
            """
        )
    )
    mapping = row.mappings().one_or_none()
    if mapping is None:
        return None
    return IndexMeta(
        fingerprint=str(mapping["fingerprint"]),
        embedder=str(mapping["embedder"]),
        model=str(mapping["model"]),
        dim=int(mapping["dim"]),
        storage=str(mapping["storage"]),
        index_kind=str(mapping["index_kind"]),
        updated_at=mapping["updated_at"],
    )


async def check_index(session: AsyncSession, settings: Settings) -> IndexState:
    """Compare database column, HNSW index, and metadata against active Settings."""
    storage, dim = await column_type(session)
    hnsw_exists = await has_hnsw_index(session)
    actual_index_kind = "hnsw" if hnsw_exists else "none"
    expected_index_kind = settings.resolved_embedding_index
    expected_fp = settings.embedding_fingerprint

    meta = await read_meta(session)
    if meta is None:
        count_row = await session.execute(text("SELECT COUNT(*) FROM chunks"))
        chunk_count = int(count_row.scalar_one())
        if chunk_count == 0:
            if (
                storage == settings.embedding_storage
                and dim == settings.embedding_dim
                and actual_index_kind == expected_index_kind
            ):
                return "missing_meta"
            return "mismatch"
        inferred_fp = HASHING_DEFAULT_FINGERPRINT
        if (
            inferred_fp == expected_fp
            and storage == settings.embedding_storage
            and dim == settings.embedding_dim
            and actual_index_kind == expected_index_kind
        ):
            return "ok"
        return "mismatch"

    if (
        meta.fingerprint != expected_fp
        or meta.storage != settings.embedding_storage
        or meta.dim != settings.embedding_dim
        or meta.index_kind != expected_index_kind
        or storage != settings.embedding_storage
        or dim != settings.embedding_dim
        or actual_index_kind != expected_index_kind
    ):
        return "mismatch"
    return "ok"


__all__ = [
    "HASHING_DEFAULT_FINGERPRINT",
    "IndexMeta",
    "IndexState",
    "check_index",
    "column_type",
    "has_hnsw_index",
    "read_meta",
]
