"""Optional cross-encoder reranker HTTP client (Spec T3.6)."""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from typing import Any

import httpx

from app.config import Settings
from app.redact import redact_text


class RerankError(Exception):
    """Raised when the upstream reranker fails, times out, or returns invalid data."""

    def __init__(self, code: str = "rerank_unavailable", detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


class RerankResultItem(dict[str, Any]):
    """Rerank result item supporting dict keys ('index', 'relevance_score', 'score'),
    attribute access (.index, .relevance_score, .score), and (index, score) unpacking.
    """

    def __init__(self, index: int, relevance_score: float) -> None:
        score = float(relevance_score)
        super().__init__(index=int(index), relevance_score=score, score=score)

    @property
    def index(self) -> int:
        return int(self["index"])

    @property
    def relevance_score(self) -> float:
        return float(self["relevance_score"])

    @property
    def score(self) -> float:
        return float(self["relevance_score"])

    def __iter__(self) -> Iterator[Any]:
        yield int(self["index"])
        yield float(self["relevance_score"])


class RerankClient:
    """OpenAI/Cohere/Jina/Magpie-compatible HTTP `/rerank` client with outbound PII redaction."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url or not base_url.strip():
            raise ValueError("base_url is required for RerankClient")
        if not model or not model.strip():
            raise ValueError("model is required for RerankClient")
        self.base_url = base_url.strip().rstrip("/")
        self.model = model.strip()
        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self.timeout_seconds = float(timeout_seconds)
        self._transport = transport
        self._external_client = client

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        *,
        top_n: int | None = None,
    ) -> list[RerankResultItem]:
        if not documents:
            return []

        redacted_query, _ = redact_text(str(query))
        redacted_docs: list[str] = []
        for doc in documents:
            cleaned_doc, _ = redact_text(str(doc))
            redacted_docs.append(cleaned_doc)

        effective_top_n = len(redacted_docs) if top_n is None else min(int(top_n), len(redacted_docs))
        payload: dict[str, Any] = {
            "model": self.model,
            "query": redacted_query,
            "documents": redacted_docs,
            "top_n": effective_top_n,
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        url = f"{self.base_url}/rerank"
        try:
            if self._external_client is not None:
                response = await self._external_client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            else:
                async with httpx.AsyncClient(
                    timeout=self.timeout_seconds,
                    transport=self._transport,
                ) as http_client:
                    response = await http_client.post(
                        url,
                        json=payload,
                        headers=headers,
                    )
        except httpx.TimeoutException as exc:
            raise RerankError("rerank_timeout", f"Rerank request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RerankError("rerank_http_error", f"Rerank HTTP transport error: {exc}") from exc
        except Exception as exc:
            if isinstance(exc, RerankError):
                raise
            raise RerankError("rerank_unavailable", str(exc)) from exc

        if response.status_code != 200:
            raise RerankError(
                f"http_{response.status_code}",
                f"Rerank endpoint returned HTTP {response.status_code}",
            )

        try:
            body = response.json()
        except Exception as exc:
            raise RerankError("invalid_json", "Rerank response is not valid JSON") from exc

        if not isinstance(body, dict):
            raise RerankError("invalid_response", "Rerank response body must be a JSON object")

        raw_list = body.get("results")
        if raw_list is None:
            raw_list = body.get("data")
        if not isinstance(raw_list, list):
            raise RerankError(
                "invalid_response",
                "Rerank response must contain a 'results' or 'data' list",
            )

        n_docs = len(redacted_docs)
        parsed: list[RerankResultItem] = []
        seen_indices: set[int] = set()
        for entry in raw_list:
            if not isinstance(entry, dict):
                raise RerankError("invalid_response", "Each rerank result must be an object")
            raw_idx = entry.get("index")
            if isinstance(raw_idx, bool) or not isinstance(raw_idx, int):
                raise RerankError("invalid_response", "Rerank result 'index' must be an integer")
            if raw_idx < 0 or raw_idx >= n_docs:
                raise RerankError(
                    "invalid_response",
                    f"Rerank result index {raw_idx} out of range (0..{n_docs - 1})",
                )
            if raw_idx in seen_indices:
                raise RerankError(
                    "invalid_response",
                    f"Duplicate rerank result index {raw_idx}",
                )
            raw_score = entry.get("relevance_score")
            if raw_score is None:
                raw_score = entry.get("score")
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                raise RerankError(
                    "invalid_response",
                    "Rerank result must include numeric 'relevance_score' or 'score'",
                )
            score_val = float(raw_score)
            if not math.isfinite(score_val):
                raise RerankError("invalid_response", "Rerank score must be finite")
            seen_indices.add(raw_idx)
            parsed.append(RerankResultItem(index=raw_idx, relevance_score=score_val))

        expected_count = n_docs if top_n is None else min(max(int(top_n), 0), n_docs)
        if len(parsed) != expected_count:
            raise RerankError(
                "invalid_response",
                f"Rerank response returned {len(parsed)} results, expected {expected_count}",
            )

        parsed.sort(key=lambda item: (-item.relevance_score, item.index))
        return parsed


_default_rerank_client: RerankClient | None = None
_default_transport: httpx.AsyncBaseTransport | None = None


def set_rerank_client(client: RerankClient | None) -> None:
    global _default_rerank_client
    _default_rerank_client = client


def set_rerank_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _default_transport
    _default_transport = transport


def build_rerank_client(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RerankClient:
    base_url = settings.rerank_base_url or settings.llm_base_url or ""
    model = settings.rerank_model or ""
    api_key = (
        settings.rerank_api_key.get_secret_value()
        if settings.rerank_api_key is not None
        else (
            settings.llm_api_key.get_secret_value()
            if settings.llm_api_key is not None
            else None
        )
    )
    return RerankClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=float(settings.rerank_timeout_seconds),
        transport=transport if transport is not None else _default_transport,
    )


def get_rerank_client(settings: Settings) -> RerankClient:
    if _default_rerank_client is not None:
        return _default_rerank_client
    return build_rerank_client(settings)


__all__ = [
    "RerankClient",
    "RerankError",
    "RerankResultItem",
    "build_rerank_client",
    "get_rerank_client",
    "set_rerank_client",
    "set_rerank_transport",
]
