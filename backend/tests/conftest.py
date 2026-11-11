"""Test environment. Uses the compose Postgres test database and never downloads weights."""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

_ENV = Path(__file__).resolve().parents[2] / ".env"
_password = os.environ.get("POSTGRES_PASSWORD", "labagent")
_redis_password = os.environ.get("REDIS_PASSWORD", "")
if _ENV.is_file():
    for line in _ENV.read_text(encoding="utf-8").splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            _password = line.split("=", 1)[1].strip()
        elif line.startswith("REDIS_PASSWORD="):
            _redis_password = line.split("=", 1)[1].strip()

os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    f"postgresql+asyncpg://labagent:{_password}@127.0.0.1:55432/labagent_test",
)
_default_redis_url = (
    f"redis://:{_redis_password}@127.0.0.1:56379/1"
    if _redis_password
    else "redis://127.0.0.1:56379/1"
)
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL", _default_redis_url)
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
os.environ["INGEST_WORKER_ENABLED"] = "false"
os.environ["OTEL_EXPORTER"] = "none"

_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="lab_agent_pytest_"))
atexit.register(shutil.rmtree, _TEST_DATA_DIR, True)
_TEST_UPLOAD_DIR = _TEST_DATA_DIR / "uploads"
_TEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_TEST_BOOTSTRAP_TOKEN_FILE = _TEST_DATA_DIR / "bootstrap_admin.token"
os.environ["UPLOAD_DIR"] = str(_TEST_UPLOAD_DIR)
os.environ["BOOTSTRAP_TOKEN_FILE"] = str(_TEST_BOOTSTRAP_TOKEN_FILE)

from app.config import reset_settings_cache  # noqa: E402

reset_settings_cache()


@pytest.fixture(scope="session", autouse=True)
def migrated() -> None:
    import asyncio

    from app.db import dispose_engine, get_sessionmaker

    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    command.upgrade(config, "head")

    async def _reset_vector_col() -> None:
        maker = get_sessionmaker()
        async with maker() as session:
            await session.execute(text("TRUNCATE chunks CASCADE"))
            await session.execute(text("DROP INDEX IF EXISTS chunks_embedding_hnsw"))
            await session.execute(
                text("ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(512)")
            )
            await session.execute(
                text(
                    "CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
                )
            )
            await session.commit()
        await dispose_engine()

    asyncio.run(_reset_vector_col())


_TABLES = (
    "audit_events",
    "messages",
    "conversations",
    "api_tokens",
    "revoked_tokens",
    "field_provenance",
    "lab_invoices",
    "lab_facilities",
    "lab_projects",
    "lab_achievements",
    "lab_people",
    "import_jobs",
    "embedding_index_meta",
    "chunks",
    "document_acl",
    "documents",
    "group_members",
    "groups",
    "users",
)
_EXISTS_SQL = text("SELECT " + " OR ".join(f"EXISTS (SELECT 1 FROM {t})" for t in _TABLES))
_TRUNCATE_SQL = text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")


def _clear_test_data_dir() -> None:
    if _TEST_UPLOAD_DIR.exists():
        for entry in _TEST_UPLOAD_DIR.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    else:
        _TEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    _TEST_BOOTSTRAP_TOKEN_FILE.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
async def clean_db() -> AsyncIterator[None]:
    from app.db import get_sessionmaker
    from app.llm import CAPTURED

    maker = get_sessionmaker()
    async with maker() as session:
        dirty = (await session.execute(_EXISTS_SQL)).scalar()
        if dirty:
            await session.execute(_TRUNCATE_SQL)
            await session.commit()
    _clear_test_data_dir()
    CAPTURED.clear()
    yield
    _clear_test_data_dir()
    CAPTURED.clear()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from app.main import app

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield http
