"""Embedding providers. The default hasher never downloads weights."""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.rag.redact import redact_text
from app.rag.tokenize import char_bigrams, tokenize


class EmbeddingError(RuntimeError):
    """Structured error raised when an embedding call fails."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class Embedder(Protocol):
    name: str
    kind: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def aclose(self) -> None: ...


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


class HashingEmbedder:
    """Signed feature hashing of jieba tokens and character bigrams."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.name = "hashing-cjk-v1"
        self.kind = "hashing"

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    async def aclose(self) -> None:
        return None

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        features = tokenize(text) + char_bigrams(text)
        if not features:
            features = ["__empty__"]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class BGEEmbedder:
    """Optional BAAI/bge-small-zh-v1.5. Imported only when selected."""

    def __init__(self, model_name: str, dim: int) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "EMBEDDER=bge requires the optional extra: uv sync --extra bge"
            ) from exc
        self.model = SentenceTransformer(model_name)
        self.name = model_name
        self.kind = "bge"
        self.dim = dim
        probe = self.model.encode(["维度检查"], normalize_embeddings=True)[0]
        if len(probe) != dim:
            raise RuntimeError(
                f"{model_name} produced dimension {len(probe)}, column is {dim}. "
                "bge-m3 needs a migration; this build is fixed at 512."
            )

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = await asyncio.to_thread(
            self.model.encode, texts, normalize_embeddings=True
        )
        return [list(map(float, row)) for row in vectors]

    async def embed_query(self, text: str) -> list[float]:
        vectors = await asyncio.to_thread(
            self.model.encode, [text], normalize_embeddings=True
        )
        return [float(value) for value in vectors[0]]

    async def aclose(self) -> None:
        return None


class OpenAICompatibleEmbedder:
    """OpenAI-compatible /v1/embeddings client (used with host Ollama for Qwen3-Embedding-8B)."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        retry_base_delay: float = 0.2,
    ) -> None:
        self.settings = settings
        self.name = settings.embedding_model
        self.kind = "openai_compatible"
        self.dim = settings.embedding_dim
        self.native_dim = settings.embedding_native_dim
        self.truncate = settings.embedding_truncate
        self.send_dimensions = settings.embedding_send_dimensions
        self.batch_size = settings.embedding_batch_size
        self.max_retries = settings.embedding_max_retries
        self.redact = settings.embedding_redact
        self.document_prefix = settings.embedding_document_prefix
        self.base_url = (settings.embedding_base_url or "").rstrip("/")
        self._retry_base_delay = retry_base_delay
        self._owns_client = client is None
        self._client = (
            client
            if client is not None
            else httpx.AsyncClient(
                timeout=httpx.Timeout(float(settings.embedding_timeout_seconds), connect=10.0)
            )
        )

    def __repr__(self) -> str:
        return (
            f"OpenAICompatibleEmbedder(model={self.name!r}, dim={self.dim}, "
            f"base_url={self.base_url!r})"
        )

    def _prepare_document(self, text: str) -> str:
        raw = text if text.strip() else "__empty__"
        if self.redact:
            raw, _ = redact_text(raw)
        if self.document_prefix:
            return f"{self.document_prefix}{raw}"
        return raw

    def _prepare_query(self, text: str) -> str:
        raw = text if text.strip() else "__empty__"
        if self.redact:
            raw, _ = redact_text(raw)
        return self.settings.render_query(raw)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        prepared = [self._prepare_document(item) for item in texts]
        results: list[list[float]] = []
        for start in range(0, len(prepared), self.batch_size):
            batch = prepared[start : start + self.batch_size]
            results.extend(await self._embed_batch(batch))
        return results

    async def embed_query(self, text: str) -> list[float]:
        prepared = self._prepare_query(text)
        vectors = await self._embed_batch([prepared])
        return vectors[0]

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        url = f"{self.base_url}/embeddings"
        payload: dict[str, Any] = {
            "model": self.name,
            "input": batch,
            "encoding_format": "float",
        }
        if self.send_dimensions:
            payload["dimensions"] = self.dim

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.settings.embedding_api_key is not None:
            headers["Authorization"] = (
                f"Bearer {self.settings.embedding_api_key.get_secret_value()}"
            )

        response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(self._retry_base_delay * (2**attempt))
                    continue
                raise EmbeddingError("timeout") from exc
            except httpx.RequestError as exc:
                if attempt < self.max_retries:
                    await asyncio.sleep(self._retry_base_delay * (2**attempt))
                    continue
                raise EmbeddingError("conn_refused") from exc

            status = response.status_code
            if status == 200:
                break
            if status in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                delay = self._retry_delay(response, attempt)
                await asyncio.sleep(delay)
                continue
            raise EmbeddingError(f"http_{status}")

        assert response is not None
        try:
            body = response.json()
        except Exception as exc:
            raise EmbeddingError("bad_response") from exc
        if not isinstance(body, dict):
            raise EmbeddingError("bad_response")
        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingError("bad_response")

        indexed: list[tuple[int, list[float]]] = []
        seen_indices: set[int] = set()
        for item in data:
            if not isinstance(item, dict):
                raise EmbeddingError("bad_response")
            idx = item.get("index")
            raw_vec = item.get("embedding")
            if (
                not isinstance(idx, int)
                or isinstance(idx, bool)
                or idx < 0
                or idx >= len(batch)
                or idx in seen_indices
                or not isinstance(raw_vec, list)
            ):
                raise EmbeddingError("bad_response")
            seen_indices.add(idx)
            try:
                vec = [float(val) for val in raw_vec]
            except (TypeError, ValueError) as exc:
                raise EmbeddingError("bad_response") from exc
            indexed.append((idx, self._validate_and_normalize(vec)))

        indexed.sort(key=lambda pair: pair[0])
        return [vec for _, vec in indexed]

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                parsed = float(retry_after.strip())
                if parsed >= 0:
                    return parsed
            except ValueError:
                pass
        return self._retry_base_delay * (2**attempt)

    def _validate_and_normalize(self, vector: list[float]) -> list[float]:
        allowed_dims = {self.dim}
        if self.native_dim is not None:
            allowed_dims.add(self.native_dim)
        if len(vector) not in allowed_dims:
            raise EmbeddingError("dimension_mismatch")
        if self.truncate:
            if len(vector) < self.dim:
                raise EmbeddingError("dimension_mismatch")
            vector = vector[: self.dim]
        elif len(vector) != self.dim:
            raise EmbeddingError("dimension_mismatch")
        return _l2_normalize(vector)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedder == "hashing":
        return HashingEmbedder(settings.embedding_dim)
    if settings.embedder == "bge":
        return BGEEmbedder(settings.embedding_model, settings.embedding_dim)
    if settings.embedder == "openai_compatible":
        return OpenAICompatibleEmbedder(settings)
    raise RuntimeError(f"Unknown embedder {settings.embedder}")


__all__ = [
    "BGEEmbedder",
    "Embedder",
    "EmbeddingError",
    "HashingEmbedder",
    "OpenAICompatibleEmbedder",
    "build_embedder",
]
