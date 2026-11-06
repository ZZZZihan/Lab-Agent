"""Embedding index rebuild CLI (`python -m app.reindex`)."""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.config import Settings, get_settings
from app.db import dispose_engine, get_sessionmaker
from app.ingest_worker import process_next_pending
from app.logging_conf import configure_logging, get_logger
from app.rag.embedder import Embedder, build_embedder
from app.rag.index_meta import (
    HASHING_DEFAULT_FINGERPRINT,
    actual_index_kind,
    check_index,
    column_type,
    read_meta,
)
from app.rag.ingest import ALLOWED_STORAGES

log = get_logger("reindex")


def _slugify(fp: str) -> str:
    return re.sub(r"[^a-z0-9]", "-", fp.lower())[:200]


async def apply_reindex(
    session: AsyncSession,
    settings: Settings,
    embedder: Embedder,
    *,
    if_needed: bool = False,
    force: bool = False,
) -> bool:
    """Rebuild the chunks embedding column, index, metadata, and document chunks in one transaction."""
    state = await check_index(session, settings)
    if if_needed and not force and state == "ok":
        return False

    storage = settings.embedding_storage
    if storage not in ALLOWED_STORAGES:
        raise ValueError(f"Unsupported embedding storage: {storage!r}")
    dim = int(settings.embedding_dim)
    index_kind = settings.resolved_embedding_index

    prev_meta = await read_meta(session)
    if prev_meta is not None:
        old_slug = prev_meta.slug[:200]
    else:
        count_row = await session.execute(text("SELECT COUNT(*) FROM chunks"))
        if int(count_row.scalar_one()) > 0:
            old_slug = _slugify(HASHING_DEFAULT_FINGERPRINT)
        else:
            old_slug = "none"

    cur_storage, cur_dim = await column_type(session)

    await session.execute(text("DROP INDEX IF EXISTS chunks_embedding_hnsw"))
    await session.execute(text("DROP INDEX IF EXISTS chunks_embedding_prefix_hnsw"))
    await session.execute(text("DELETE FROM chunks"))

    if cur_storage != storage or cur_dim != dim:
        await session.execute(
            text(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE {storage}({dim})")
        )

    if index_kind == "hnsw":
        await session.execute(
            text(
                f"CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding {storage}_cosine_ops)"
            )
        )
    elif index_kind == "hnsw_prefix":
        prefix_dim = int(settings.embedding_prefix_dim)
        await session.execute(
            text(
                f"CREATE INDEX chunks_embedding_prefix_hnsw ON chunks USING hnsw "
                f"((subvector(embedding, 1, {prefix_dim})::halfvec({prefix_dim})) halfvec_cosine_ops)"
            )
        )
        await session.execute(
            text(
                "ALTER TABLE embedding_index_meta "
                "DROP CONSTRAINT IF EXISTS embedding_index_meta_index_kind"
            )
        )
        await session.execute(
            text(
                "ALTER TABLE embedding_index_meta "
                "ADD CONSTRAINT embedding_index_meta_index_kind "
                "CHECK (index_kind IN ('hnsw', 'hnsw_prefix', 'none'))"
            )
        )
    else:
        log.info(
            "embedding_index_skipped",
            reason="维度超过索引上限，使用精确检索"
            if settings.embedding_index == "auto"
            else "EMBEDDING_INDEX=none，使用精确检索",
            storage=storage,
            dim=dim,
        )

    model_name = "hashing-cjk-v1" if settings.embedder == "hashing" else settings.embedding_model
    await session.execute(
        text(
            """
            INSERT INTO embedding_index_meta (id, fingerprint, embedder, model, dim, storage, index_kind, updated_at)
            VALUES (1, :fingerprint, :embedder, :model, :dim, :storage, :index_kind, now())
            ON CONFLICT (id) DO UPDATE SET
                fingerprint = EXCLUDED.fingerprint,
                embedder = EXCLUDED.embedder,
                model = EXCLUDED.model,
                dim = EXCLUDED.dim,
                storage = EXCLUDED.storage,
                index_kind = EXCLUDED.index_kind,
                updated_at = now()
            """
        ),
        {
            "fingerprint": settings.embedding_fingerprint,
            "embedder": settings.embedder,
            "model": model_name,
            "dim": dim,
            "storage": storage,
            "index_kind": index_kind,
        },
    )

    res = await session.execute(
        text(
            """
            UPDATE documents
               SET status = 'pending',
                   error_code = NULL,
                   attempts = 0,
                   updated_at = now()
             WHERE deleted_at IS NULL
            """
        )
    )
    doc_count = int(getattr(res, "rowcount", 0) or 0)
    await session.commit()

    new_slug = settings.embedding_slug[:200]
    await write_audit(
        settings=settings,
        actor_id=None,
        action="index_rebuilt",
        resource_type="embedding_index",
        detail={
            "from": old_slug,
            "to": new_slug,
            "documents": doc_count,
            "index": index_kind,
        },
    )
    return True


async def _run(check_only: bool, apply: bool, if_needed: bool, force: bool) -> int:
    settings = get_settings()
    configure_logging()
    maker = get_sessionmaker()

    try:
        if check_only:
            async with maker() as session:
                state = await check_index(session, settings)
                col_storage, col_dim = await column_type(session)
                idx_kind = await actual_index_kind(session)
                meta = await read_meta(session)
                print(
                    f"state={state} fingerprint={settings.embedding_fingerprint} "
                    f"column={col_storage}({col_dim}) index={idx_kind} "
                    f"meta_fingerprint={meta.fingerprint if meta else 'none'}"
                )
            return 0 if state == "ok" else 2

        if apply:
            embedder = build_embedder(settings)
            try:
                async with maker() as session:
                    rebuilt = await apply_reindex(
                        session,
                        settings,
                        embedder,
                        if_needed=if_needed,
                        force=force,
                    )
                if rebuilt:
                    while await process_next_pending(embedder, settings) is not None:
                        pass
                print(
                    f"rebuilt={str(rebuilt).lower()} fingerprint={settings.embedding_fingerprint} "
                    f"storage={settings.embedding_storage}({settings.embedding_dim}) "
                    f"index={settings.resolved_embedding_index}"
                )
            finally:
                await embedder.aclose()
            return 0
    finally:
        await dispose_engine()

    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect or rebuild the embedding index.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Check whether the index matches settings")
    group.add_argument("--apply", action="store_true", help="Rebuild the embedding index")
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help="Skip rebuild when the index state is already ok",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild even when --if-needed is passed and state is ok",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(
            check_only=args.check,
            apply=args.apply,
            if_needed=args.if_needed,
            force=args.force,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
