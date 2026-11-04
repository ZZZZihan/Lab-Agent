"""Runtime settings. Secrets come only from the environment and fail closed."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDERS = {
    "changeme",
    "change-me",
    "change_me",
    "secret",
    "password",
    "jwt-secret",
    "jwt_secret",
    "dev-secret",
    "replace-me",
    "your-secret-here",
    "insecure",
    "placeholder",
}

MODES = (
    "dense",
    "hybrid_rrf",
    "hybrid_lexical_rerank",
    "hybrid_lexical_rerank15",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def assert_strong_secret(value: str, name: str) -> None:
    stripped = value.strip()
    lowered = stripped.lower()
    if len(stripped) < 32:
        raise RuntimeError(f"{name} must be at least 32 characters")
    if lowered in PLACEHOLDERS or "replace-me" in lowered or "changeme" in lowered:
        raise RuntimeError(f"{name} is a placeholder and cannot be used")
    if lowered.startswith("your-"):
        raise RuntimeError(f"{name} is a placeholder and cannot be used")
    if len(set(stripped)) < 10:
        raise RuntimeError(f"{name} has too little character variety")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(repo_root() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    database_url: str
    redis_url: str = "redis://127.0.0.1:56379/0"
    jwt_secret: str
    jwt_ttl_minutes: int = 480
    cors_origins: str = "http://127.0.0.1:43123,http://localhost:43123"
    llm_provider: Literal["fake", "openai", "deepseek", "ollama", "vllm"] = "fake"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str = "fake-grounded-v1"
    embedder: Literal["hashing", "bge"] = "hashing"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    reranker: Literal["off", "lexical", "bge"] = "off"
    reranker_model: str = "BAAI/bge-reranker-base"
    retrieval_mode: str | None = None
    dense_pool: int | None = None
    lexical_pool: int | None = None
    rrf_k: int | None = None
    evidence_score_min: float | None = None
    cite_margin: float | None = None
    cite_k: int | None = None
    rare_coverage_min: float | None = None
    load_tuned_config: bool = True
    tuned_config_path: str = ""
    corpus_dir: str = ""
    seed_only_dir: str = ""
    questions_path: str = ""
    audit_retention_days: int = 180
    upload_dir: str = "./data/uploads"
    max_upload_bytes: int = 5_000_000
    otel_exporter: Literal["none", "console", "otlp"] = "none"
    otel_endpoint: str | None = None
    rate_limit_enabled: bool = True
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 60
    chat_rate_limit: int = 30
    chat_rate_window_seconds: int = 60
    llm_capture: bool = False
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_json: str | None = None
    paper_search_provider: Literal["unconfigured"] = "unconfigured"
    agent_max_steps: int = 4
    bm25_k1: float = 1.2
    bm25_b: float = 0.75
    service_name: str = "lab-agent-api"

    @model_validator(mode="after")
    def _enforce(self) -> Self:
        assert_strong_secret(self.jwt_secret, "JWT_SECRET")
        if self.embedding_dim != 512:
            raise RuntimeError(
                "EMBEDDING_DIM must stay 512 so hashing and bge-small-zh-v1.5 share one column"
            )
        origins = [part.strip() for part in self.cors_origins.split(",") if part.strip()]
        if not origins or any(origin == "*" for origin in origins):
            raise RuntimeError("CORS_ORIGINS must be an explicit list and cannot contain *")
        self.cors_origins = ",".join(origins)
        if self.llm_provider not in {"fake", "ollama"} and not (
            self.llm_api_key and len(self.llm_api_key) >= 8
        ):
            raise RuntimeError(f"LLM_API_KEY is required for provider {self.llm_provider}")
        if self.llm_base_url is None:
            defaults = {
                "openai": "https://api.openai.com/v1",
                "deepseek": "https://api.deepseek.com/v1",
                "ollama": "http://127.0.0.1:11434/v1",
            }
            if self.llm_provider in defaults:
                self.llm_base_url = defaults[self.llm_provider]
            elif self.llm_provider == "vllm":
                raise RuntimeError("LLM_BASE_URL is required for vLLM")
        if self.llm_capture and self.app_env != "test":
            raise RuntimeError("LLM_CAPTURE is only allowed when APP_ENV=test")
        if not 1 <= self.audit_retention_days <= 3650:
            raise RuntimeError("AUDIT_RETENTION_DAYS must be between 1 and 3650")
        if self.oidc_issuer and not self.oidc_audience:
            raise RuntimeError("OIDC_AUDIENCE is required when OIDC_ISSUER is set")
        if self.app_env == "test":
            self.rate_limit_enabled = False
        root = repo_root()
        if not self.corpus_dir:
            self.corpus_dir = str(root / "evals" / "data" / "v1" / "corpus")
        if not self.seed_only_dir:
            self.seed_only_dir = str(root / "evals" / "data" / "v1" / "seed_only")
        if not self.questions_path:
            self.questions_path = str(root / "evals" / "data" / "v1" / "questions.jsonl")
        tuned: dict[str, object] = {}
        path = (
            Path(self.tuned_config_path)
            if self.tuned_config_path
            else root / "evals" / "data" / "v1" / "selected_config.json"
        )
        if self.load_tuned_config and path.is_file():
            tuned = json.loads(path.read_text(encoding="utf-8"))
        self.retrieval_mode = _pick(self.retrieval_mode, tuned, "retrieval_mode", "dense")
        self.dense_pool = int(_pick(self.dense_pool, tuned, "dense_pool", 80))
        self.lexical_pool = int(_pick(self.lexical_pool, tuned, "lexical_pool", 80))
        self.rrf_k = int(_pick(self.rrf_k, tuned, "rrf_k", 60))
        # Missing calibration fails closed: refuse rather than answer on an untuned score.
        self.evidence_score_min = float(
            _pick(self.evidence_score_min, tuned, "evidence_score_min", 0.99)
        )
        self.cite_margin = float(_pick(self.cite_margin, tuned, "cite_margin", 0.15))
        self.cite_k = int(_pick(self.cite_k, tuned, "cite_k", 3))
        self.rare_coverage_min = float(
            _pick(self.rare_coverage_min, tuned, "rare_coverage_min", 0.0)
        )
        if self.retrieval_mode not in MODES:
            raise RuntimeError(f"Unknown retrieval mode: {self.retrieval_mode}")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [part.strip() for part in self.cors_origins.split(",") if part.strip()]


def _pick[T](current: T | None, tuned: dict[str, object], key: str, default: T) -> T:
    if current is not None:
        return current
    if key not in tuned:
        return default
    value = tuned[key]
    if isinstance(default, float):
        return cast(T, float(str(value)))
    if isinstance(default, int) and not isinstance(default, bool):
        return cast(T, int(str(value)))
    return cast(T, value)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


# Imported by modules that want a single name without calling into cache mechanics.
def reset_settings_cache() -> None:
    get_settings.cache_clear()


# Field is re-exported for callers that build partial models in tests.
__all__ = [
    "Field",
    "MODES",
    "Settings",
    "assert_strong_secret",
    "get_settings",
    "repo_root",
    "reset_settings_cache",
]
