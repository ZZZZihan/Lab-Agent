"""Pure checks: secrets, redaction, chunk citations, refusal score, dev-only tuning."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from app.audit import AuditRejected, sanitize_detail
from app.config import Settings, assert_strong_secret
from app.rag.chunker import chunk_markdown, parse_front_matter
from app.rag.redact import redact_text
from app.rag.rrf import Hit, decide
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
            database_url="postgresql+asyncpg://yanzheng:yanzheng@localhost/yanzheng",
            jwt_secret="replace-me-with-a-long-random-string",
            cors_origins="http://localhost:43123",
            load_tuned_config=False,
        )


def test_wildcard_cors_rejected() -> None:
    with pytest.raises(RuntimeError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database_url="postgresql+asyncpg://yanzheng:yanzheng@localhost/yanzheng",
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
