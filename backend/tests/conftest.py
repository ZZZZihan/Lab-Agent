"""Test environment. Uses the compose Postgres test database and never downloads weights."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

_ENV = Path(__file__).resolve().parents[2] / ".env"
_password = "yanzheng"
if _ENV.is_file():
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            _password = line.split("=", 1)[1].strip()

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    f"postgresql+asyncpg://yanzheng:{_password}@127.0.0.1:55432/yanzheng_test",
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:56379/1")
os.environ["JWT_SECRET"] = "test-only-jwt-secret-value-0123456789-abcdef"
os.environ["LOAD_TUNED_CONFIG"] = "false"
os.environ["EVIDENCE_SCORE_MIN"] = "0"
os.environ["RARE_COVERAGE_MIN"] = "0"
os.environ["CITE_MARGIN"] = "1"
os.environ["CITE_K"] = "4"
os.environ["RRF_K"] = "60"
os.environ["RETRIEVAL_MODE"] = "dense"
os.environ["LLM_CAPTURE"] = "true"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["EMBEDDER"] = "hashing"
os.environ["CORS_ORIGINS"] = "http://127.0.0.1:43123"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["OTEL_EXPORTER"] = "none"

from app.config import reset_settings_cache  # noqa: E402

reset_settings_cache()


@pytest.fixture(scope="session", autouse=True)
def migrated() -> None:
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")


@pytest.fixture(autouse=True)
async def clean_db() -> AsyncIterator[None]:
    from app.db import dispose_engine, get_sessionmaker
    from app.llm import CAPTURED

    # Each test may run on a fresh loop. Drop pooled asyncpg connections first.
    await dispose_engine()
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(
            text(
                "TRUNCATE audit_events, messages, conversations, revoked_tokens, "
                "chunks, document_acl, documents, users RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
    CAPTURED.clear()
    yield
    CAPTURED.clear()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
