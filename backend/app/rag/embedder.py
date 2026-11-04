"""Embedding providers. The default hasher never downloads weights."""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Protocol

from app.rag.tokenize import char_bigrams, tokenize


class Embedder(Protocol):
    name: str
    kind: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def aclose(self) -> None: ...


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


def build_embedder(kind: str, model_name: str, dim: int) -> Embedder:
    if kind == "hashing":
        return HashingEmbedder(dim)
    if kind == "bge":
        return BGEEmbedder(model_name, dim)
    raise RuntimeError(f"Unknown embedder {kind}")
