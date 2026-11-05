"""Lab Agent API entrypoint."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, MutableMapping
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.ingest_worker import recover_processing, run_worker
from app.llm import build_llm
from app.logging_conf import configure_logging, get_logger
from app.maintenance import run_maintenance_worker
from app.otel import setup_telemetry
from app.rag.embedder import build_embedder
from app.rag.index_meta import check_index, read_meta
from app.ratelimit import RateLimiter
from app.routes import install_exception_handler, router
from app.routes_admin import router as admin_router

log = get_logger("api")

ScopeState = MutableMapping[str, object]
Message = MutableMapping[str, object]


class RequestContextMiddleware:
    """Attach a request id without BaseHTTPMiddleware.

    Starlette's ``BaseHTTPMiddleware`` runs the endpoint in a child task. asyncpg
    connections are bound to the loop that checked them out, and that split
    raises "Future attached to a different loop" under pytest-asyncio.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        raw_headers = scope.get("headers")
        header_pairs = raw_headers if isinstance(raw_headers, list) else []
        for key, value in header_pairs:
            if key == b"x-request-id" and value:
                request_id = value.decode("latin-1")
                break
        state: ScopeState = scope.setdefault("state", {})
        state["request_id"] = request_id

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = message.get("headers")
                headers = list(raw) if isinstance(raw, list) else []
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            log.info(
                "http_request",
                method=scope.get("method"),
                path=scope.get("path"),
                request_id=request_id,
            )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    app.state.settings = settings
    app.state.embedder = build_embedder(settings)
    app.state.llm = build_llm(settings)
    app.state.limiter = RateLimiter(settings)
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
            idx_state = await check_index(session, settings)
            meta = await read_meta(session)
    except Exception:
        log.error("database_unreachable")
        raise
    if idx_state == "mismatch":
        await app.state.embedder.aclose()
        await app.state.limiter.close()
        raise RuntimeError(
            f"数据库中的向量索引 ({meta.fingerprint if meta else 'unknown'}) 与当前配置 "
            f"({settings.embedding_fingerprint}) 不一致，请先执行 python -m app.reindex --apply"
        )
    if idx_state == "missing_meta":
        log.warning(
            "embedding_index_meta_missing",
            fingerprint=settings.embedding_fingerprint,
        )
    log.info(
        "startup",
        env=settings.app_env,
        embedder=settings.embedder,
        llm_provider=settings.llm_provider,
        retrieval_mode=settings.retrieval_mode,
    )
    worker_stop: asyncio.Event | None = None
    worker_task: asyncio.Task[None] | None = None
    if settings.ingest_worker_enabled:
        await recover_processing()
        worker_stop = asyncio.Event()
        worker_task = asyncio.create_task(run_worker(app.state.embedder, settings, worker_stop))
    maintenance_stop: asyncio.Event | None = None
    maintenance_task: asyncio.Task[None] | None = None
    if settings.maintenance_interval_seconds > 0 and settings.app_env != "test":
        maintenance_stop = asyncio.Event()
        maintenance_task = asyncio.create_task(
            run_maintenance_worker(settings, maintenance_stop)
        )
    try:
        yield
    finally:
        if maintenance_stop is not None and maintenance_task is not None:
            maintenance_stop.set()
            await maintenance_task
        if worker_stop is not None and worker_task is not None:
            worker_stop.set()
            await worker_task
        await app.state.embedder.aclose()
        await app.state.llm.aclose()
        await app.state.limiter.close()
        await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Lab Agent", version="0.1.0", lifespan=lifespan)
    # Last added middleware is outermost. Request id is set before CORS and routes.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
    )
    app.add_middleware(RequestContextMiddleware)
    setup_telemetry(app, settings)
    app.include_router(router)
    app.include_router(admin_router)
    install_exception_handler(app)
    return app


app = create_app()
