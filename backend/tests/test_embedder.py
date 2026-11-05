"""Unit tests for OpenAICompatibleEmbedder, Settings embedding validation, and embed_check."""

from __future__ import annotations

import json
import math
import sys
import types
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.config import Settings
from app.embed_check import (
    ANCHOR_QUERY,
    DETERMINISM_QUERY,
    PARAPHRASE_DOC,
    PARAPHRASE_QUERY,
    UNRELATED_DOC,
    UNRELATED_QUERY,
    run_embed_check,
)
from app.rag.embedder import BGEEmbedder, EmbeddingError, OpenAICompatibleEmbedder, build_embedder

JWT_SECRET = "test-only-jwt-secret-value-0123456789-abcdef"
DB_URL = "postgresql+asyncpg://labagent:labagent@localhost/labagent"


def _make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "_env_file": None,
        "app_env": "test",
        "database_url": DB_URL,
        "jwt_secret": JWT_SECRET,
        "cors_origins": "http://localhost:43123",
        "load_tuned_config": False,
        "embedder": "openai_compatible",
        "embedding_base_url": "http://127.0.0.1:11434/v1",
        "embedding_model": "qwen3-embedding:8b",
        "embedding_model_version": "64b933495768",
        "embedding_dim": 64,
        "embedding_native_dim": 64,
        "embedding_truncate": False,
        "embedding_storage": "vector",
        "embedding_index": "auto",
        "embedding_batch_size": 4,
        "embedding_max_retries": 2,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_qwen3_query_template_and_document_no_prefix() -> None:
    instruction = "Given a question from a research lab member, retrieve the lab document passage that answers it"
    settings = _make_settings(
        embedding_query_template=r"Instruct: {instruction}\nQuery:{query}",
        embedding_query_instruction=instruction,
        embedding_document_prefix="",
    )
    rendered = settings.render_query("GPU 预约")
    assert rendered == f"Instruct: {instruction}\nQuery:GPU 预约"
    assert "\nQuery: " not in rendered

    embedder = OpenAICompatibleEmbedder(settings)
    assert embedder._prepare_query("GPU 预约") == f"Instruct: {instruction}\nQuery:GPU 预约"
    assert embedder._prepare_document("文档正文") == "文档正文"


def test_bge_style_prefix_configuration() -> None:
    settings = _make_settings(
        embedding_model="BAAI/bge-large-zh-v1.5",
        embedding_query_template="为这个句子生成表示以用于检索相关文章：{query}",
        embedding_query_instruction="",
        embedding_document_prefix="",
    )
    embedder = OpenAICompatibleEmbedder(settings)
    assert embedder._prepare_query("检索问题") == "为这个句子生成表示以用于检索相关文章：检索问题"
    assert embedder._prepare_document("段落") == "段落"


async def test_batching_and_out_of_order_response_sorting() -> None:
    requests_seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests_seen.append(body)
        inputs: list[str] = body["input"]
        # Return out-of-order indices intentionally; embedder must sort by index.
        data = []
        for idx, text in reversed(list(enumerate(inputs))):
            tag = float(int(text.split("-")[1]))
            vec = [tag] + [0.0] * 63
            data.append({"index": idx, "embedding": vec})
        return httpx.Response(200, json={"data": data})

    settings = _make_settings(embedding_batch_size=3, embedding_dim=64, embedding_native_dim=64)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = OpenAICompatibleEmbedder(settings, client=client)

    docs = [f"doc-{i}" for i in range(1, 8)]
    vectors = await embedder.embed_documents(docs)
    assert len(requests_seen) == 3
    assert [len(req["input"]) for req in requests_seen] == [3, 3, 1]
    assert len(vectors) == 7
    # Since each input has [i, 0, ...], L2 normalization yields [1.0, 0.0, ...] for positive i.
    # Let's verify exact order using distinct 2D directions!
    await client.aclose()


async def test_out_of_order_index_restores_exact_vector_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        data = []
        for idx in reversed(range(len(inputs))):
            vec = [0.0] * 64
            vec[idx] = 5.0  # distinct axis per item index
            data.append({"index": idx, "embedding": vec})
        return httpx.Response(200, json={"data": data})

    settings = _make_settings(embedding_batch_size=4, embedding_dim=64, embedding_native_dim=64)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = OpenAICompatibleEmbedder(settings, client=client)
    vectors = await embedder.embed_documents(["a", "b", "c"])
    assert vectors[0][0] == pytest.approx(1.0)
    assert vectors[1][1] == pytest.approx(1.0)
    assert vectors[2][2] == pytest.approx(1.0)
    await client.aclose()


async def test_mrl_truncation_and_l2_normalization() -> None:
    # Server returns 128-dim (native_dim=128) or already truncated 64-dim (dim=64)
    raw_128 = [float(i + 1) for i in range(128)]

    def handler_128(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": raw_128}]})

    settings = _make_settings(
        embedding_dim=64,
        embedding_native_dim=128,
        embedding_truncate=True,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler_128))
    embedder = OpenAICompatibleEmbedder(settings, client=client)
    vec = await embedder.embed_query("问题")
    assert len(vec) == 64
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=1e-6)
    expected_norm = math.sqrt(sum(x * x for x in raw_128[:64]))
    assert vec[0] == pytest.approx(raw_128[0] / expected_norm)
    await client.aclose()

    # Also accepts already-truncated 64-dim response from server
    def handler_64(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": raw_128[:64]}]})

    client_64 = httpx.AsyncClient(transport=httpx.MockTransport(handler_64))
    embedder_64 = OpenAICompatibleEmbedder(settings, client=client_64)
    vec_64 = await embedder_64.embed_query("问题")
    assert len(vec_64) == 64
    assert vec_64 == pytest.approx(vec)
    await client_64.aclose()


async def test_dimension_mismatch_raises_embedding_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 96}]})

    settings = _make_settings(
        embedding_dim=64,
        embedding_native_dim=128,
        embedding_truncate=True,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = OpenAICompatibleEmbedder(settings, client=client)
    with pytest.raises(EmbeddingError) as exc_info:
        await embedder.embed_query("测试")
    assert exc_info.value.code == "dimension_mismatch"
    await client.aclose()


async def test_send_dimensions_parameter_toggle() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 64}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder_off = OpenAICompatibleEmbedder(
        _make_settings(embedding_send_dimensions=False), client=client
    )
    await embedder_off.embed_query("q1")
    assert "dimensions" not in payloads[-1]

    embedder_on = OpenAICompatibleEmbedder(
        _make_settings(embedding_send_dimensions=True), client=client
    )
    await embedder_on.embed_query("q2")
    assert payloads[-1]["dimensions"] == 64
    await client.aclose()


async def test_retry_on_429_and_503_and_no_retry_on_400_401() -> None:
    attempts = 0

    def handler_retry(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        if attempts == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0] * 64}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler_retry))
    embedder = OpenAICompatibleEmbedder(
        _make_settings(embedding_max_retries=2),
        client=client,
        retry_base_delay=0.001,
    )
    vec = await embedder.embed_query("重试")
    assert len(vec) == 64
    assert attempts == 3
    await client.aclose()

    for status_code in (400, 401):
        calls = 0

        def handler_fail(_request: httpx.Request, code: int = status_code) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(code)

        fail_client = httpx.AsyncClient(transport=httpx.MockTransport(handler_fail))
        fail_embedder = OpenAICompatibleEmbedder(
            _make_settings(embedding_max_retries=3),
            client=fail_client,
            retry_base_delay=0.001,
        )
        with pytest.raises(EmbeddingError) as exc_info:
            await fail_embedder.embed_query("不重试")
        assert exc_info.value.code == f"http_{status_code}"
        assert calls == 1
        await fail_client.aclose()


async def test_empty_input_and_redaction_and_secret_not_leaked(tmp_path: Path) -> None:
    seen_inputs: list[list[str]] = []
    seen_auth: list[str | None] = []
    secret_val = "ci-only-jwt-secret-value-0123456789-abcdef"
    key_file = tmp_path / "embed.key"
    key_file.write_text(secret_val + "\n", encoding="utf-8")
    key_file.chmod(0o600)

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization"))
        body = json.loads(request.content.decode("utf-8"))
        seen_inputs.append(body["input"])
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [1.0] * 64} for i in range(len(body["input"]))]},
        )

    settings = _make_settings(
        embedding_api_key=None,
        embedding_api_key_file=str(key_file),
        embedding_redact=True,
    )
    assert secret_val not in repr(settings)
    assert secret_val not in str(settings.model_dump())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    embedder = OpenAICompatibleEmbedder(settings, client=client)
    assert secret_val not in repr(embedder)

    assert await embedder.embed_documents([]) == []
    assert seen_inputs == []

    await embedder.embed_documents(["   ", "联系 sec@qingheng.lab 处理"])
    assert seen_inputs[-1][0] == "__empty__"
    assert "sec@qingheng.lab" not in seen_inputs[-1][1]
    assert "[REDACTED_EMAIL]" in seen_inputs[-1][1]
    assert seen_auth[-1] == f"Bearer {secret_val}"
    await client.aclose()


def test_settings_validation_and_fingerprint_change_rules() -> None:
    # Missing base_url when openai_compatible
    with pytest.raises(RuntimeError, match="EMBEDDING_BASE_URL"):
        _make_settings(embedding_base_url=None)

    # Default BGE model left unchanged when openai_compatible
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL"):
        _make_settings(embedding_model="BAAI/bge-small-zh-v1.5")

    # Truncate=false when dim < native_dim
    with pytest.raises(RuntimeError, match="EMBEDDING_TRUNCATE"):
        _make_settings(embedding_dim=1024, embedding_native_dim=4096, embedding_truncate=False)

    # Dim > native_dim
    with pytest.raises(RuntimeError, match="EMBEDDING_DIM cannot exceed EMBEDDING_NATIVE_DIM"):
        _make_settings(embedding_dim=2048, embedding_native_dim=1024, embedding_truncate=True)

    # vector(4096) + hnsw rejected
    with pytest.raises(RuntimeError, match="EMBEDDING_INDEX=hnsw is not supported"):
        _make_settings(
            embedding_dim=4096,
            embedding_native_dim=4096,
            embedding_storage="vector",
            embedding_index="hnsw",
        )

    # halfvec(4096) + hnsw rejected (max 4000)
    with pytest.raises(RuntimeError, match="EMBEDDING_INDEX=hnsw is not supported"):
        _make_settings(
            embedding_dim=4096,
            embedding_native_dim=4096,
            embedding_storage="halfvec",
            embedding_index="hnsw",
        )

    # APP_ENV=prod requires EMBEDDING_MODEL_VERSION when embedder != hashing
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL_VERSION"):
        _make_settings(
            app_env="prod",
            internal_proxy_secret="ci-only-jwt-secret-value-0123456789-abcdef",
            embedding_model_version="",
        )

    # Template without {query}
    with pytest.raises(RuntimeError, match="must contain \\{query\\}"):
        _make_settings(embedding_query_template="Instruct: only")

    # Fingerprint changes whenever model, version, dim, storage, redact, instruction, or doc prefix changes
    base = _make_settings()
    fp_base = base.embedding_fingerprint
    assert _make_settings(embedding_model="qwen3-embedding:4b").embedding_fingerprint != fp_base
    assert _make_settings(embedding_model_version="9704fd987c12").embedding_fingerprint != fp_base
    assert _make_settings(embedding_dim=128, embedding_native_dim=128).embedding_fingerprint != fp_base
    assert _make_settings(embedding_storage="halfvec").embedding_fingerprint != fp_base
    assert _make_settings(embedding_redact=True).embedding_fingerprint != fp_base
    assert (
        _make_settings(
            embedding_query_template=r"Instruct: {instruction}\nQuery:{query}",
            embedding_query_instruction="Retrieve passage",
        ).embedding_fingerprint
        != fp_base
    )
    assert _make_settings(embedding_document_prefix="passage: ").embedding_fingerprint != fp_base


async def test_embed_check_success_and_failure() -> None:
    def make_vec(axis: int) -> list[float]:
        v = [0.0] * 64
        v[axis] = 1.0
        return v

    def handler_ok(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        data = []
        for idx, text in enumerate(inputs):
            if ANCHOR_QUERY in text or PARAPHRASE_QUERY in text or PARAPHRASE_DOC in text:
                vec = make_vec(0)
            elif UNRELATED_QUERY in text or UNRELATED_DOC in text:
                vec = make_vec(1)
            elif DETERMINISM_QUERY in text:
                vec = make_vec(2)
            else:
                vec = make_vec(3)
            data.append({"index": idx, "embedding": vec})
        return httpx.Response(200, json={"data": data})

    settings = _make_settings()
    client_ok = httpx.AsyncClient(transport=httpx.MockTransport(handler_ok))
    embedder_ok = OpenAICompatibleEmbedder(settings, client=client_ok)
    ok, summary = await run_embed_check(settings=settings, embedder=embedder_ok)
    assert ok is True
    assert summary.startswith("ok ")
    assert "dim=64" in summary
    await client_ok.aclose()

    # Low similarity gap fails embed_check
    def handler_bad_gap(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        inputs: list[str] = body["input"]
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": make_vec(0)} for i in range(len(inputs))]},
        )

    client_bad = httpx.AsyncClient(transport=httpx.MockTransport(handler_bad_gap))
    embedder_bad = OpenAICompatibleEmbedder(settings, client=client_bad)
    ok_bad, summary_bad = await run_embed_check(settings=settings, embedder=embedder_bad)
    assert ok_bad is False
    assert summary_bad.startswith("fail ")
    await client_bad.aclose()

    # build_embedder dispatches openai_compatible
    built = build_embedder(settings)
    assert isinstance(built, OpenAICompatibleEmbedder)
    await built.aclose()


async def test_bge_embedder_applies_query_template_not_on_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded_inputs: list[list[str]] = []
    init_calls: list[dict[str, Any]] = []

    class FakeSentenceTransformer:
        def __init__(self, model_name: str, **kwargs: Any) -> None:
            init_calls.append({"model_name": model_name, **kwargs})

        def encode(self, texts: list[str], *, normalize_embeddings: bool = False) -> list[list[float]]:
            assert normalize_embeddings is True
            encoded_inputs.append(list(texts))
            return [[1.0] + [0.0] * 63 for _ in texts]

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    instruction = "為這個句子生成表示以用於檢索相關文章："
    settings = _make_settings(
        embedder="bge",
        embedding_base_url=None,
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_model_version="a1b2c3d4e5f67890",
        embedding_dim=64,
        embedding_native_dim=None,
        embedding_query_template=r"Instruct: {instruction}\nQuery:{query}",
        embedding_query_instruction=instruction,
        embedding_document_prefix="",
        embedding_redact=False,
    )

    embedder = build_embedder(settings)
    assert isinstance(embedder, BGEEmbedder)
    assert init_calls == [
        {
            "model_name": "BAAI/bge-small-zh-v1.5",
            "device": "cpu",
            "revision": "a1b2c3d4e5f67890",
        }
    ]
    assert encoded_inputs == [["维度检查"]]

    await embedder.embed_query("GPU 预约入口")
    assert encoded_inputs[-1] == [f"Instruct: {instruction}\nQuery:GPU 预约入口"]

    await embedder.embed_documents(["第一篇文档正文", "第二篇文档正文"])
    assert encoded_inputs[-1] == ["第一篇文档正文", "第二篇文档正文"]

    # Also verify optional document_prefix and redaction when configured
    settings_prefixed = _make_settings(
        embedder="bge",
        embedding_base_url=None,
        embedding_model="BAAI/bge-small-zh-v1.5",
        embedding_model_version="",
        embedding_dim=64,
        embedding_native_dim=None,
        embedding_query_template="query: {query}",
        embedding_document_prefix="passage: ",
        embedding_redact=True,
    )
    embedder_prefixed = BGEEmbedder(settings_prefixed)
    assert init_calls[-1] == {"model_name": "BAAI/bge-small-zh-v1.5", "device": "cpu"}

    await embedder_prefixed.embed_query("联系 sec@qingheng.lab")
    assert encoded_inputs[-1] == ["query: 联系 [REDACTED_EMAIL]"]

    await embedder_prefixed.embed_documents(["   ", "邮箱 sec@qingheng.lab"])
    assert encoded_inputs[-1] == ["passage: __empty__", "passage: 邮箱 [REDACTED_EMAIL]"]
    assert await embedder_prefixed.embed_documents([]) == []
    await embedder.aclose()
    await embedder_prefixed.aclose()

