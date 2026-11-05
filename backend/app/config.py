"""Runtime settings. Secrets come only from the environment and fail closed."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.secrets import read_secret

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

REDACTION_VERSION = 2


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


def extract_host_port(url: str | None) -> str | None:
    if not url or not url.strip():
        return None
    raw = url.strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urllib.parse.urlsplit(raw)
    host = parsed.hostname
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return host
    if port is not None:
        return f"{host}:{port}"
    return host


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(repo_root() / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["dev", "test", "prod"] = "dev"
    database_url: str
    redis_url: str = "redis://127.0.0.1:56379/0"
    jwt_secret: SecretStr
    jwt_ttl_minutes: int = 480
    cors_origins: str = "http://127.0.0.1:43123,http://localhost:43123"
    llm_provider: Literal[
        "fake", "openai", "deepseek", "ollama", "vllm", "openai_compatible"
    ] = "fake"
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_api_key_file: str = ""
    llm_model: str = "fake-grounded-v1"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.1
    llm_max_tokens: int = 800
    llm_redact: bool = True
    embedder: Literal["hashing", "bge", "openai_compatible"] = "hashing"
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None
    embedding_api_key_file: str = ""
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_model_version: str = ""
    embedding_dim: int = 512
    embedding_native_dim: int | None = None
    embedding_truncate: bool = False
    embedding_storage: Literal["vector", "halfvec"] = "vector"
    embedding_index: Literal["auto", "hnsw", "none"] = "auto"
    embedding_send_dimensions: bool = False
    embedding_batch_size: int = 16
    embedding_timeout_seconds: int = 120
    embedding_max_retries: int = 3
    embedding_query_template: str = "{query}"
    embedding_query_instruction: str = ""
    embedding_document_prefix: str = ""
    embedding_redact: bool = False
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
    calibration_dir: str = "./data/calibrations"
    calibration_source: str | None = None
    corpus_dir: str = ""
    seed_only_dir: str = ""
    questions_path: str = ""
    audit_retention_days: int = 180
    deleted_document_retention_days: int = 30
    message_retention_days: int = 180
    maintenance_interval_seconds: int = 86400
    upload_dir: str = "./data/uploads"
    max_upload_bytes: int = 5 * 1024 * 1024
    max_document_chars: int = 300_000
    ingest_worker_enabled: bool = True
    ingest_max_attempts: int = 3
    ingest_poll_seconds: float = 1.0
    otel_exporter: Literal["none", "console", "otlp"] = "none"
    otel_endpoint: str | None = None
    internal_proxy_secret: SecretStr | None = None
    audit_hmac_key: SecretStr | None = None
    rate_limit_enabled: bool = True
    login_ip_rate_limit: int = 30
    login_ip_rate_window_seconds: int = 60
    login_account_rate_limit: int = 10
    login_account_rate_window_seconds: int = 900
    chat_rate_limit: int = 30
    chat_rate_window_seconds: int = 60
    upload_rate_limit: int = 30
    upload_rate_window_seconds: int = 3600
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
        assert_strong_secret(self.jwt_secret.get_secret_value(), "JWT_SECRET")
        if (
            self.internal_proxy_secret is not None
            and not self.internal_proxy_secret.get_secret_value().strip()
        ):
            self.internal_proxy_secret = None
        if self.app_env == "prod" and self.internal_proxy_secret is None:
            raise RuntimeError("INTERNAL_PROXY_SECRET is required when APP_ENV=prod")
        if self.internal_proxy_secret is not None:
            assert_strong_secret(
                self.internal_proxy_secret.get_secret_value(), "INTERNAL_PROXY_SECRET"
            )
        if (
            self.audit_hmac_key is not None
            and not self.audit_hmac_key.get_secret_value().strip()
        ):
            self.audit_hmac_key = None
        if self.app_env == "prod" and self.audit_hmac_key is None:
            raise RuntimeError("AUDIT_HMAC_KEY is required when APP_ENV=prod")
        if self.audit_hmac_key is not None:
            assert_strong_secret(
                self.audit_hmac_key.get_secret_value(), "AUDIT_HMAC_KEY"
            )
        elif self.app_env == "dev":
            warnings.warn(
                "AUDIT_HMAC_KEY is not set; falling back to JWT_SECRET for IP hashing",
                UserWarning,
                stacklevel=2,
            )
        if not 64 <= self.embedding_dim <= 4096:
            raise RuntimeError("EMBEDDING_DIM must be between 64 and 4096")
        if self.embedder == "hashing":
            self.embedding_native_dim = None
        if self.embedding_native_dim is not None:
            if not 64 <= self.embedding_native_dim <= 16000:
                raise RuntimeError("EMBEDDING_NATIVE_DIM must be between 64 and 16000")
            if self.embedding_dim > self.embedding_native_dim:
                raise RuntimeError("EMBEDDING_DIM cannot exceed EMBEDDING_NATIVE_DIM")
            if self.embedding_dim < self.embedding_native_dim and not self.embedding_truncate:
                raise RuntimeError(
                    "EMBEDDING_TRUNCATE must be true when EMBEDDING_DIM < EMBEDDING_NATIVE_DIM"
                )
        max_hnsw_dim = 2000 if self.embedding_storage == "vector" else 4000
        if self.embedding_index == "hnsw" and self.embedding_dim > max_hnsw_dim:
            raise RuntimeError(
                f"EMBEDDING_INDEX=hnsw is not supported for {self.embedding_storage}({self.embedding_dim}) "
                f"(max {max_hnsw_dim})"
            )
        self.embedding_query_template = self.embedding_query_template.replace("\\n", "\n")
        if "{query}" not in self.embedding_query_template:
            raise RuntimeError("EMBEDDING_QUERY_TEMPLATE must contain {query}")
        try:
            self.embedding_query_template.format(
                instruction=self.embedding_query_instruction,
                query="",
            )
        except Exception as exc:
            raise RuntimeError("Invalid EMBEDDING_QUERY_TEMPLATE format") from exc
        if self.embedding_batch_size < 1:
            raise RuntimeError("EMBEDDING_BATCH_SIZE must be at least 1")
        if self.embedding_timeout_seconds <= 0:
            raise RuntimeError("EMBEDDING_TIMEOUT_SECONDS must be positive")
        if self.embedding_max_retries < 0:
            raise RuntimeError("EMBEDDING_MAX_RETRIES cannot be negative")
        self.embedding_api_key = read_secret(
            self.embedding_api_key.get_secret_value()
            if self.embedding_api_key is not None
            else None,
            self.embedding_api_key_file or None,
            "EMBEDDING_API_KEY",
            app_env=self.app_env,
        )
        if self.embedder == "openai_compatible":
            if not self.embedding_base_url or not self.embedding_base_url.strip():
                raise RuntimeError("EMBEDDING_BASE_URL is required when EMBEDDER=openai_compatible")
            self.embedding_base_url = self.embedding_base_url.strip()
            if (
                not self.embedding_model.strip()
                or self.embedding_model.strip() == "BAAI/bge-small-zh-v1.5"
            ):
                raise RuntimeError(
                    "EMBEDDING_MODEL must be explicitly set when EMBEDDER=openai_compatible"
                )
            self.embedding_model = self.embedding_model.strip()
        if (
            self.app_env == "prod"
            and self.embedder != "hashing"
            and not self.embedding_model_version.strip()
        ):
            raise RuntimeError(
                "EMBEDDING_MODEL_VERSION is required when APP_ENV=prod and EMBEDDER is not hashing"
            )
        origins = [part.strip() for part in self.cors_origins.split(",") if part.strip()]
        if not origins or any(origin == "*" for origin in origins):
            raise RuntimeError("CORS_ORIGINS must be an explicit list and cannot contain *")
        self.cors_origins = ",".join(origins)
        if self.llm_provider not in {"fake", "ollama"} or self.llm_api_key is not None:
            self.llm_api_key = read_secret(
                self.llm_api_key.get_secret_value() if self.llm_api_key is not None else None,
                self.llm_api_key_file or None,
                "LLM_API_KEY",
                app_env=self.app_env,
            )
        if self.llm_provider not in {"fake", "ollama"} and not (
            self.llm_api_key and len(self.llm_api_key.get_secret_value()) >= 8
        ):
            raise RuntimeError(f"LLM_API_KEY is required for provider {self.llm_provider}")
        if self.llm_base_url is None or not self.llm_base_url.strip():
            defaults = {
                "openai": "https://api.openai.com/v1",
                "deepseek": "https://api.deepseek.com/v1",
                "ollama": "http://127.0.0.1:11434/v1",
            }
            if self.llm_provider in defaults:
                self.llm_base_url = defaults[self.llm_provider]
            elif self.llm_provider == "vllm":
                raise RuntimeError("LLM_BASE_URL is required for vLLM")
            elif self.llm_provider == "openai_compatible":
                raise RuntimeError("LLM_BASE_URL is required for openai_compatible")
        if self.llm_provider == "openai_compatible":
            if not self.llm_model.strip() or self.llm_model.strip() == "fake-grounded-v1":
                raise RuntimeError("LLM_MODEL must be explicitly set for openai_compatible")
        if self.llm_capture and self.app_env != "test":
            raise RuntimeError("LLM_CAPTURE is only allowed when APP_ENV=test")
        if not 1 <= self.audit_retention_days <= 3650:
            raise RuntimeError("AUDIT_RETENTION_DAYS must be between 1 and 3650")
        if not 1 <= self.deleted_document_retention_days <= 3650:
            raise RuntimeError("DELETED_DOCUMENT_RETENTION_DAYS must be between 1 and 3650")
        if not 1 <= self.message_retention_days <= 3650:
            raise RuntimeError("MESSAGE_RETENTION_DAYS must be between 1 and 3650")
        if self.maintenance_interval_seconds < 0:
            raise RuntimeError("MAINTENANCE_INTERVAL_SECONDS cannot be negative")
        if self.oidc_issuer and not self.oidc_audience:
            raise RuntimeError("OIDC_AUDIENCE is required when OIDC_ISSUER is set")
        if self.app_env == "test" and "rate_limit_enabled" not in self.model_fields_set:
            self.rate_limit_enabled = False
        root = repo_root()
        if not self.corpus_dir:
            self.corpus_dir = str(root / "evals" / "data" / "v1" / "corpus")
        if not self.seed_only_dir:
            self.seed_only_dir = str(root / "evals" / "data" / "v1" / "seed_only")
        if not self.questions_path:
            self.questions_path = str(root / "evals" / "data" / "v1" / "questions.jsonl")
        slug = self.embedding_slug
        explicit_env = self.evidence_score_min is not None
        tuned: dict[str, object] = {}
        self.calibration_source = None
        if self.load_tuned_config:
            candidates = [
                Path(self.calibration_dir) / f"{slug}.json",
                root / "evals" / "calibrations" / f"{slug}.json",
            ]
            found_path: Path | None = None
            for candidate in candidates:
                if candidate.is_file():
                    found_path = candidate
                    break
            if found_path is not None:
                try:
                    loaded = json.loads(found_path.read_text(encoding="utf-8"))
                except Exception:
                    loaded = None
                if (
                    isinstance(loaded, dict)
                    and loaded.get("embedder_fingerprint") == self.embedding_fingerprint
                ):
                    tuned = loaded
                    self.calibration_source = str(found_path)
        if explicit_env:
            self.calibration_source = "env"
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
    def calibrated(self) -> bool:
        return self.calibration_source is not None

    @property
    def resolved_audit_hmac_key(self) -> str:
        if self.audit_hmac_key is not None:
            return self.audit_hmac_key.get_secret_value()
        return self.jwt_secret.get_secret_value()

    @property
    def cors_origin_list(self) -> list[str]:
        return [part.strip() for part in self.cors_origins.split(",") if part.strip()]

    @property
    def llm_base_host(self) -> str | None:
        return extract_host_port(self.llm_base_url)

    def render_query(self, query: str) -> str:
        return self.embedding_query_template.format(
            instruction=self.embedding_query_instruction,
            query=query,
        )

    @property
    def resolved_embedding_index(self) -> Literal["hnsw", "none"]:
        if self.embedding_index == "none":
            return "none"
        if self.embedding_index == "hnsw":
            return "hnsw"
        max_hnsw_dim = 2000 if self.embedding_storage == "vector" else 4000
        return "hnsw" if self.embedding_dim <= max_hnsw_dim else "none"

    @property
    def embedding_fingerprint(self) -> str:
        if self.embedder == "hashing":
            model_id = "hashing-cjk-v1"
        else:
            version = self.embedding_model_version.strip()
            model_id = (
                f"{self.embedding_model}@{version[:12]}" if version else self.embedding_model
            )
        storage_suffix = "h" if self.embedding_storage == "halfvec" else ""
        redact_flag = (
            "0"
            if self.embedder == "hashing" or not self.embedding_redact
            else f"v{REDACTION_VERSION}"
        )
        qp_text = "" if self.embedder == "hashing" else self.render_query("")
        dp_text = "" if self.embedder == "hashing" else self.embedding_document_prefix
        qp_hash = hashlib.sha256(qp_text.encode("utf-8")).hexdigest()[:8]
        dp_hash = hashlib.sha256(dp_text.encode("utf-8")).hexdigest()[:8]
        return (
            f"{self.embedder}|{model_id}|{self.embedding_dim}{storage_suffix}"
            f"|redact={redact_flag}|qp={qp_hash}|dp={dp_hash}"
        )

    @property
    def embedding_slug(self) -> str:
        return re.sub(r"[^a-z0-9]", "-", self.embedding_fingerprint.lower())

    @property
    def embedding_base_host(self) -> str | None:
        if self.embedder != "openai_compatible" or not self.embedding_base_url:
            return None
        from urllib.parse import urlparse

        parsed = urlparse(self.embedding_base_url)
        return parsed.netloc or None


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
    "REDACTION_VERSION",
    "Settings",
    "assert_strong_secret",
    "extract_host_port",
    "get_settings",
    "repo_root",
    "reset_settings_cache",
]
