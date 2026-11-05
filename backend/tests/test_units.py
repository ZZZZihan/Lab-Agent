"""Pure checks: secrets, redaction, chunk citations, refusal score, dev-only tuning."""

from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest
from app.audit import AuditRejected, sanitize_detail
from app.auth import UNUSABLE_PASSWORD_HASH, verify_password
from app.config import Settings, assert_strong_secret, get_settings
from app.rag.chunker import chunk_markdown, parse_front_matter
from app.rag.embedder import BGEEmbedder, HashingEmbedder
from app.rag.redact import redact_text
from app.rag.rrf import Hit, decide
from app.secrets import read_secret
from app.seed import assert_demo_seed_allowed
from evals.metrics import tune

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = (
    "本科生",
    "电费",
    "CMTEB",
    "报销",
    "工资",
    "磁带",
    "拍照",
    "银行",
    "root",
    "明年",
    "抽样比例",
    "申请表",
    "录播",
    "许可证",
    "双选",
    "分机",
    "版本号",
    "口令",
)


def test_weak_secrets_rejected() -> None:
    with pytest.raises(RuntimeError):
        assert_strong_secret("changeme", "JWT_SECRET")
    with pytest.raises(RuntimeError):
        assert_strong_secret("short-secret", "JWT_SECRET")
    with pytest.raises(RuntimeError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
            jwt_secret="replace-me-with-a-long-random-string",
            cors_origins="http://localhost:43123",
            load_tuned_config=False,
        )


def test_prod_refuses_demo_seed() -> None:
    assert_demo_seed_allowed("dev")
    assert_demo_seed_allowed("test")
    with pytest.raises(SystemExit):
        assert_demo_seed_allowed("prod")


def test_wildcard_cors_rejected() -> None:
    with pytest.raises(RuntimeError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
            jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",
            cors_origins="*",
            load_tuned_config=False,
        )


def test_redacts_contact_identifiers() -> None:
    text = "联系 sec@qingheng.lab 或 13812345678，身份证 11010119900307887X。"
    cleaned, count = redact_text(text)
    assert count == 3
    assert "sec@qingheng.lab" not in cleaned
    assert "13812345678" not in cleaned
    assert "11010119900307887X" not in cleaned
    assert "[REDACTED_EMAIL]" in cleaned


def test_audit_rejects_prompt_text() -> None:
    with pytest.raises(AuditRejected):
        sanitize_detail({"prompt": "用户的原问题"})
    sanitize_detail({"model": "fake-grounded-v1", "prompt_tokens": 12, "hit_ids": ["abc"]})


def test_chunker_keeps_heading_with_its_fact() -> None:
    body = "前言。\n\n## 硬件规模\n\n共有 8 台节点。\n\n## 预约\n\n最长 24 小时。\n"
    chunks = chunk_markdown(body)
    assert [chunk.heading for chunk in chunks] == ["概述", "硬件规模", "预约"]
    assert "8 台" in chunks[1].text
    assert "24 小时" not in chunks[1].text
    assert "24 小时" in chunks[2].text


def test_corpus_headings_are_unique_and_bait_terms_absent() -> None:
    corpus = ROOT / "evals" / "data" / "v1" / "corpus"
    files = list(corpus.glob("*.md"))
    assert len(files) >= 12
    for file in files:
        meta, body = parse_front_matter(file.read_text(encoding="utf-8"))
        assert meta.get("key")
        headings = [chunk.heading for chunk in chunk_markdown(body)]
        assert len(headings) == len(set(headings))
        for term in FORBIDDEN:
            assert term not in body, f"{file.name} contains {term}"
    questions = (ROOT / "evals" / "data" / "v1" / "questions.jsonl").read_text(encoding="utf-8")
    assert questions.count("\n") >= 60
    for term in ("本科生", "CMTEB", "报销码", "磁带"):
        assert term in questions


def test_refusal_uses_cosine_not_a_normalized_fusion_score() -> None:
    hit: Hit = {
        "key": "doc#1",
        "chunk_id": "c",
        "document_id": "d",
        "cosine": 0.18,
        "bm25": 50.0,
        "rrf": 0.99,
        "content": "共享词",
        "tokens": ["共享词"],
        "heading": "硬件规模",
        "title": "t",
        "chunk_index": 1,
    }
    decision = decide(
        [hit],
        mode="hybrid_rrf",
        score_min=0.45,
        rare_coverage=1.0,
        rare_min=0.0,
        margin=0.1,
        cite_k=2,
    )
    assert decision["refused"] is True
    assert decision["reason"] == "low_score"
    assert decision["citations"] == []


def test_tuner_rejects_test_rows() -> None:
    with pytest.raises(RuntimeError, match="dev split"):
        tune([{"split": "test", "hits": [], "gold": [], "answerable": False, "query_tokens": [], "df": {}, "n_docs": 1, "elapsed_ms": 1}], "dense")


def test_app_import_does_not_pull_torch() -> None:
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "app" / "rag" / "embedder.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] != "torch" for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("sentence_transformers") or isinstance(
                node, ast.ImportFrom
            )
    # sentence-transformers may appear only inside a function.
    source = (root / "app" / "rag" / "embedder.py").read_text(encoding="utf-8")
    assert "import sentence_transformers" not in source.split("def __init__")[0]


def test_secret_str_not_exposed_in_repr_or_dump() -> None:
    jwt_plain = "test-only-jwt-secret-value-0123456789-abcdef"
    llm_plain = "ci-only-jwt-secret-value-0123456789-abcdef"
    default_settings = get_settings()
    assert jwt_plain not in repr(default_settings)
    assert jwt_plain not in str(default_settings.model_dump())

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
        jwt_secret=jwt_plain,  # type: ignore[arg-type]
        llm_provider="openai",
        llm_api_key=llm_plain,  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        load_tuned_config=False,
    )
    assert jwt_plain not in repr(settings)
    assert llm_plain not in repr(settings)
    assert jwt_plain not in str(settings.model_dump())
    assert llm_plain not in str(settings.model_dump())
    assert settings.jwt_secret.get_secret_value() == jwt_plain
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == llm_plain


def test_read_secret_precedence_and_error_redaction(tmp_path: Path) -> None:
    file_val = "test-only-jwt-secret-value-0123456789-abcdef"
    env_val = "ci-only-jwt-secret-value-0123456789-abcdef"
    secret_file = tmp_path / "magpie.key"
    secret_file.write_text(f"  {file_val}\n", encoding="utf-8")
    secret_file.chmod(0o600)

    resolved_env = read_secret(env_val, str(secret_file), "LLM_API_KEY")
    assert resolved_env is not None
    assert resolved_env.get_secret_value() == env_val

    resolved_file = read_secret(None, str(secret_file), "LLM_API_KEY")
    assert resolved_file is not None
    assert resolved_file.get_secret_value() == file_val

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        llm_provider="openai",
        llm_api_key=None,
        llm_api_key_file=str(secret_file),
        cors_origins="http://localhost:43123",
        load_tuned_config=False,
    )
    assert settings.llm_api_key is not None
    assert settings.llm_api_key.get_secret_value() == file_val

    empty_file = tmp_path / "empty.key"
    empty_file.write_text("   \n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="LLM_API_KEY file is empty"):
        read_secret(None, str(empty_file), "LLM_API_KEY")

    sentinel = "SENTINEL_FILE_CONTENT_DO_NOT_LEAK"
    bad_utf8_file = tmp_path / "bad_utf8.key"
    bad_utf8_file.write_bytes(sentinel.encode("utf-8") + b"\xff\xfe")
    with pytest.raises(RuntimeError) as exc_bad_utf8:
        read_secret(None, str(bad_utf8_file), "LLM_API_KEY")
    assert sentinel not in str(exc_bad_utf8.value)
    assert sentinel not in repr(exc_bad_utf8.value)
    assert "LLM_API_KEY" in str(exc_bad_utf8.value)
    assert str(bad_utf8_file) in str(exc_bad_utf8.value)

    multiline_file = tmp_path / "multiline.key"
    multiline_file.write_text(f"{sentinel}\nextra-line\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc_multiline:
        read_secret(None, str(multiline_file), "LLM_API_KEY")
    assert sentinel not in str(exc_multiline.value)
    assert sentinel not in repr(exc_multiline.value)
    assert "LLM_API_KEY" in str(exc_multiline.value)
    assert str(multiline_file) in str(exc_multiline.value)

    short_sentinel = "SNTNL7"
    short_file = tmp_path / "short.key"
    short_file.write_text(short_sentinel, encoding="utf-8")
    with pytest.raises(RuntimeError) as exc_short:
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
            jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
            llm_provider="openai",
            llm_api_key=None,
            llm_api_key_file=str(short_file),
            cors_origins="http://localhost:43123",
            load_tuned_config=False,
        )
    assert short_sentinel not in str(exc_short.value)
    assert short_sentinel not in repr(exc_short.value)

    open_file = tmp_path / "open_perms.key"
    open_file.write_text(file_val, encoding="utf-8")
    open_file.chmod(0o644)
    prod_resolved = read_secret(None, str(open_file), "LLM_API_KEY", app_env="prod")
    assert prod_resolved is not None
    assert prod_resolved.get_secret_value() == file_val


def test_unusable_password_hash_never_verifies() -> None:
    assert verify_password(UNUSABLE_PASSWORD_HASH, "!unusable") is False
    assert verify_password(UNUSABLE_PASSWORD_HASH, "") is False
    assert verify_password("!anything", "anything") is False


async def test_hashing_embedder_async_protocol() -> None:
    embedder = HashingEmbedder(dim=64)
    assert embedder.kind == "hashing"
    assert embedder.name == "hashing-cjk-v1"
    assert embedder.dim == 64
    q_vec = await embedder.embed_query("清衡-A100 节点")
    d_vecs = await embedder.embed_documents(["清衡-A100 节点", "周三组会"])
    assert len(q_vec) == 64
    assert len(d_vecs) == 2
    assert q_vec == d_vecs[0]
    await embedder.aclose()


async def test_bge_embedder_async_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], bool]] = []
    init_kwargs: list[dict[str, object]] = []

    class FakeSentenceTransformer:
        def __init__(self, model_name: str, **kwargs: object) -> None:
            self.model_name = model_name
            init_kwargs.append({"model_name": model_name, **kwargs})

        def encode(self, texts: list[str], *, normalize_embeddings: bool = False) -> list[list[float]]:
            calls.append((list(texts), normalize_embeddings))
            return [[float(idx + 1)] * 8 for idx, _ in enumerate(texts)]

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = FakeSentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    embedder = BGEEmbedder("BAAI/bge-small-zh-v1.5", 8)
    assert embedder.kind == "bge"
    assert embedder.name == "BAAI/bge-small-zh-v1.5"
    assert embedder.dim == 8
    assert init_kwargs == [{"model_name": "BAAI/bge-small-zh-v1.5", "device": "cpu"}]
    assert calls == [(["维度检查"], True)]

    q_vec = await embedder.embed_query("查询")
    d_vecs = await embedder.embed_documents(["文档一", "文档二"])
    assert q_vec == [1.0] * 8
    assert d_vecs == [[1.0] * 8, [2.0] * 8]
    assert calls[-2:] == [(["查询"], True), (["文档一", "文档二"], True)]
    await embedder.aclose()

