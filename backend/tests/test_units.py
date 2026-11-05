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
from app.rag.chunker import chunk_document, chunk_markdown, parse_front_matter
from app.rag.embedder import BGEEmbedder, HashingEmbedder
from app.rag.extract import detect_pasted_format
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
    text = (
        "联系 sec@qingheng.lab 或 13812345678，座机 010-62785432 / 075586012345，"
        "身份证 11010119900307887X，银行卡 4532015112830366 与 6222021234567890128，"
        "护照 E12345678、G87654321、P11223344。"
    )
    cleaned, count = redact_text(text)
    assert count == 10
    assert "sec@qingheng.lab" not in cleaned
    assert "13812345678" not in cleaned
    assert "010-62785432" not in cleaned
    assert "075586012345" not in cleaned
    assert "11010119900307887X" not in cleaned
    assert "4532015112830366" not in cleaned
    assert "6222021234567890128" not in cleaned
    assert "E12345678" not in cleaned
    assert "G87654321" not in cleaned
    assert "P11223344" not in cleaned
    assert "[REDACTED_EMAIL]" in cleaned
    assert cleaned.count("[REDACTED_PHONE]") == 3
    assert "[REDACTED_ID]" in cleaned
    assert cleaned.count("[REDACTED_CARD]") == 2
    assert cleaned.count("[REDACTED_PASSPORT]") == 3


def test_redact_false_positives_and_v1_corpus() -> None:
    from app.config import REDACTION_VERSION

    assert REDACTION_VERSION == 2

    # False-positive cases for each rule:
    # - IP/CIDR, dates, HX-17, version numbers, meeting IDs
    # - 16-digit number failing Luhn (4532015112830367)
    # - 9-digit / lowercase / hyphenated passport-like tokens (E123456789, e12345678, E-12345678)
    benign = (
        "内网网段 10.0.0.0/16 与 192.168.1.1/24，日期 2026-09-12 与 2026-11-05，"
        "代号 HX-17，版本 v0.10.1234567 与 0.8.0-pg16，会议室号 883-102-447，"
        "非 Luhn 16 位单号 4532015112830367，提交哈希 e12345678 与编号 E123456789、E-12345678。"
    )
    cleaned_benign, count_benign = redact_text(benign)
    assert count_benign == 0
    assert cleaned_benign == benign

    # Run redaction over the entire v1 corpus (and seed_only) and assert only expected hits
    corpus_hits: dict[str, tuple[int, str]] = {}
    for folder in ("corpus", "seed_only"):
        folder_path = ROOT / "evals" / "data" / "v1" / folder
        for md_file in sorted(folder_path.glob("*.md")):
            raw = md_file.read_text(encoding="utf-8")
            cleaned, count = redact_text(raw)
            if count > 0:
                corpus_hits[md_file.name] = (count, cleaned)
            else:
                assert cleaned == raw

    assert list(corpus_hits.keys()) == ["safety-compute.md"]
    safety_count, safety_cleaned = corpus_hits["safety-compute.md"]
    assert safety_count == 1
    assert "sec@qingheng.lab" not in safety_cleaned
    assert "[REDACTED_EMAIL]" in safety_cleaned


def test_audit_rejects_prompt_text() -> None:
    with pytest.raises(AuditRejected):
        sanitize_detail({"prompt": "用户的原问题"})
    with pytest.raises(AuditRejected):
        sanitize_detail({"note": "请联系 sec@qingheng.lab 处理"})
    with pytest.raises(AuditRejected):
        sanitize_detail({"contact": "13812345678"})
    with pytest.raises(AuditRejected):
        sanitize_detail({"contact": "010-62785432"})
    with pytest.raises(AuditRejected):
        sanitize_detail({"items": ["ok", "user@example.org"]})
    with pytest.raises(AuditRejected):
        sanitize_detail({"items": ["ok", "+86 13800138000"]})
    sanitize_detail(
        {
            "model": "fake-grounded-v1",
            "prompt_tokens": 12,
            "hit_ids": ["abc", "01234567-89ab-cdef-0123-456789abcdef"],
        }
    )


def test_chunker_keeps_heading_with_its_fact() -> None:
    body = "前言。\n\n## 硬件规模\n\n共有 8 台节点。\n\n## 预约\n\n最长 24 小时。\n"
    chunks = chunk_markdown(body)
    assert [chunk.heading for chunk in chunks] == ["概述", "硬件规模", "预约"]
    assert "8 台" in chunks[1].text
    assert "24 小时" not in chunks[1].text
    assert "24 小时" in chunks[2].text
    assert detect_pasted_format(body) == "markdown"
    assert detect_pasted_format("第一段没有二级标题。\n\n第二段也没有。") == "text"
    assert [c.heading for c in chunk_document("纯文本第一段。", "text")] == ["第 1 段"]
    assert [c.heading for c in chunk_document("第一页内容\f第二页内容", "pdf")] == [
        "第 1 页",
        "第 2 页",
    ]


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


def test_calibration_loading_mismatch_and_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    for var in (
        "EVIDENCE_SCORE_MIN",
        "RARE_COVERAGE_MIN",
        "CITE_MARGIN",
        "CITE_K",
        "RRF_K",
        "RETRIEVAL_MODE",
        "LOAD_TUNED_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)

    jwt = "test-only-jwt-secret-value-0123456789-abcdef"
    db_url = "postgresql+asyncpg://labagent:labagent@localhost/labagent"

    # 1. Repo calibration file is loaded when runtime dir has no file
    s_repo = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=db_url,
        jwt_secret=jwt,  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        load_tuned_config=True,
        calibration_dir=str(tmp_path / "empty_runtime"),
    )
    assert s_repo.calibrated is True
    assert s_repo.calibration_source is not None
    assert Path(s_repo.calibration_source).name == f"{s_repo.embedding_slug}.json"
    assert s_repo.evidence_score_min == pytest.approx(0.2475)
    assert s_repo.retrieval_mode == "dense"
    assert s_repo.cite_k == 1

    # 2. Runtime calibration directory takes precedence over repo calibration directory
    runtime_file = tmp_path / f"{s_repo.embedding_slug}.json"
    runtime_file.write_text(
        json.dumps(
            {
                "embedder_fingerprint": s_repo.embedding_fingerprint,
                "dataset": "v1",
                "split_used": "dev",
                "tuned_at": "2026-11-05T00:00:00Z",
                "git_sha": "abc1234",
                "retrieval_mode": "hybrid_rrf",
                "rrf_k": 40,
                "evidence_score_min": 0.4125,
                "rare_coverage_min": 0.1,
                "cite_margin": 0.08,
                "cite_k": 2,
                "dense_pool": 80,
                "lexical_pool": 80,
            }
        ),
        encoding="utf-8",
    )
    s_runtime = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=db_url,
        jwt_secret=jwt,  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        load_tuned_config=True,
        calibration_dir=str(tmp_path),
    )
    assert s_runtime.calibrated is True
    assert s_runtime.calibration_source == str(runtime_file)
    assert s_runtime.evidence_score_min == pytest.approx(0.4125)
    assert s_runtime.retrieval_mode == "hybrid_rrf"
    assert s_runtime.rrf_k == 40
    assert s_runtime.cite_k == 2

    # 3. Mismatched embedder_fingerprint falls back to 0.99 and calibrated=False
    runtime_file.write_text(
        json.dumps(
            {
                "embedder_fingerprint": "hashing|hashing-cjk-v1|256|redact=0|qp=e3b0c442|dp=e3b0c442",
                "retrieval_mode": "dense",
                "evidence_score_min": 0.15,
            }
        ),
        encoding="utf-8",
    )
    s_mismatch = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=db_url,
        jwt_secret=jwt,  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        load_tuned_config=True,
        calibration_dir=str(tmp_path),
    )
    assert s_mismatch.calibrated is False
    assert s_mismatch.calibration_source is None
    assert s_mismatch.evidence_score_min == pytest.approx(0.99)

    # Also test uncalibrated fingerprint with no file anywhere
    s_uncalibrated = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=db_url,
        jwt_secret=jwt,  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        embedding_dim=256,
        load_tuned_config=True,
        calibration_dir=str(tmp_path / "empty_runtime"),
    )
    assert s_uncalibrated.calibrated is False
    assert s_uncalibrated.calibration_source is None
    assert s_uncalibrated.evidence_score_min == pytest.approx(0.99)

    # 4. Explicit environment variable (EVIDENCE_SCORE_MIN) takes precedence and sets calibration_source="env"
    monkeypatch.setenv("EVIDENCE_SCORE_MIN", "0.33")
    s_env = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=db_url,
        jwt_secret=jwt,  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        embedding_dim=256,
        load_tuned_config=True,
        calibration_dir=str(tmp_path / "empty_runtime"),
    )
    assert s_env.calibrated is True
    assert s_env.calibration_source == "env"
    assert s_env.evidence_score_min == pytest.approx(0.33)


async def test_run_eval_check_exits_3_for_uncalibrated_embedder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.config import reset_settings_cache
    from evals.run_eval import _run

    monkeypatch.setenv("EMBEDDING_DIM", "256")
    reset_settings_cache()
    try:
        with pytest.raises(SystemExit) as exc_info:
            await _run(tune_dev=False, check=True)
        assert exc_info.value.code == 3
        captured = capsys.readouterr()
        assert "该嵌入器没有已提交的基线" in captured.err
    finally:
        monkeypatch.delenv("EMBEDDING_DIM", raising=False)
        reset_settings_cache()


def test_internal_proxy_secret_and_rate_limit_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.delenv("INTERNAL_PROXY_SECRET", raising=False)

    default_test = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        load_tuned_config=False,
    )
    assert default_test.rate_limit_enabled is False
    assert default_test.login_ip_rate_limit == 30
    assert default_test.login_ip_rate_window_seconds == 60
    assert default_test.login_account_rate_limit == 10
    assert default_test.login_account_rate_window_seconds == 900

    explicit_test = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        rate_limit_enabled=True,
        load_tuned_config=False,
    )
    assert explicit_test.rate_limit_enabled is True

    with pytest.raises(RuntimeError, match="INTERNAL_PROXY_SECRET is required when APP_ENV=prod"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            app_env="prod",
            database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
            jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
            cors_origins="http://localhost:43123",
            llm_capture=False,
            load_tuned_config=False,
        )

    with pytest.raises(RuntimeError, match="INTERNAL_PROXY_SECRET"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            app_env="prod",
            database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
            jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
            internal_proxy_secret="short-secret",  # type: ignore[arg-type]
            cors_origins="http://localhost:43123",
            llm_capture=False,
            load_tuned_config=False,
        )

    with pytest.raises(RuntimeError, match="AUDIT_HMAC_KEY is required when APP_ENV=prod"):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            app_env="prod",
            database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
            jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
            internal_proxy_secret="ci-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
            cors_origins="http://localhost:43123",
            llm_capture=False,
            load_tuned_config=False,
        )

    with pytest.warns(UserWarning, match="AUDIT_HMAC_KEY is not set"):
        dev_fallback = Settings(
            _env_file=None,  # type: ignore[call-arg]
            app_env="dev",
            database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
            jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
            cors_origins="http://localhost:43123",
            llm_capture=False,
            load_tuned_config=False,
        )
    assert dev_fallback.resolved_audit_hmac_key == "test-only-jwt-secret-value-0123456789-abcdef"

    prod_ok = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="prod",
        database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        internal_proxy_secret="ci-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        audit_hmac_key="ci-audit-hmac-secret-5742644509-xbekkg",  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        llm_capture=False,
        load_tuned_config=False,
    )
    assert prod_ok.internal_proxy_secret is not None
    assert prod_ok.resolved_audit_hmac_key == "ci-audit-hmac-secret-5742644509-xbekkg"


async def test_rate_limiter_login_ip_and_account_buckets() -> None:
    import hashlib

    import fakeredis.aioredis
    from app.ratelimit import RateLimiter, login_account_bucket, login_ip_bucket

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        rate_limit_enabled=True,
        load_tuned_config=False,
    )
    assert settings.rate_limit_enabled is True

    limiter = RateLimiter(settings)
    await limiter.close()
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    limiter.redis = fake_redis

    try:
        email = "  Alice@QingHeng.Lab "
        expected_acct_key = (
            f"login:acct:{hashlib.sha256(b'alice@qingheng.lab').hexdigest()[:16]}"
        )
        assert login_account_bucket(email) == expected_acct_key
        assert login_ip_bucket("203.0.113.7") == "login:ip:203.0.113.7"

        # Account bucket: 10 attempts allowed, 11th triggers "account"
        for _ in range(10):
            assert await limiter.check_login("203.0.113.7", email) is None
        assert await limiter.check_login("203.0.113.7", "alice@qingheng.lab") == "account"
        acct_ttl = await fake_redis.ttl(expected_acct_key)
        assert 0 < acct_ttl <= 900

        # A different account from the same IP is still allowed (IP count is 12 <= 30)
        assert await limiter.check_login("203.0.113.7", "bob@qingheng.lab") is None

        # IP bucket: 30 attempts across distinct accounts allowed, 31st triggers "ip"
        ip2 = "198.51.100.42"
        for i in range(30):
            acct = f"member-{i // 5}@qingheng.lab"
            assert await limiter.check_login(ip2, acct) is None
        assert await limiter.check_login(ip2, "fresh@qingheng.lab") == "ip"
        ip_ttl = await fake_redis.ttl("login:ip:198.51.100.42")
        assert 0 < ip_ttl <= 60
    finally:
        await limiter.close()


def test_smoke_parse_sse_events() -> None:
    from app.smoke import parse_sse_events

    raw = (
        ": keep-alive comment\n\n"
        'event: status\r\ndata: {"type": "status", "phase": "search_knowledge_base"}\r\n\r\n'
        'event: citations\ndata: {"type": "citations", "items": [{"document_id": "doc-1", "index": 1}]}\n\n'
        'event: token\ndata: {"type": "token", "text": "门禁每 17 天"}\n\n'
        'event: token\ndata: {"text": "轮换 [1]。"}\n\n'
        'event: citation_check\ndata: {"type": "citation_check", "used": [1], "invalid": [], "uncited": false}\n\n'
        "data: [DONE]\n\n"
        'event: done\ndata: {"type": "done", "refused": false, "conversation_id": "c1", "message_id": "m1"}\n\n'
    )
    events = parse_sse_events(raw)
    assert [e["type"] for e in events] == [
        "status",
        "citations",
        "token",
        "token",
        "citation_check",
        "done",
    ]
    assert "".join(str(e.get("text") or "") for e in events if e["type"] == "token") == (
        "门禁每 17 天轮换 [1]。"
    )
    assert events[1]["items"] == [{"document_id": "doc-1", "index": 1}]
    assert events[-1]["refused"] is False


async def test_run_smoke_offline_and_real_modes() -> None:
    import json
    import urllib.parse

    import httpx
    from app.smoke import run_smoke

    def make_transport(*, real_mode: bool) -> httpx.MockTransport:
        uploaded_docs: dict[str, dict[str, str]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/api/v1/health":
                fp = (
                    "openai_compatible|lab-agent-qwen3-embed@64b933495768|4096h|redact=0|qp=0b6f261e|dp=e3b0c442"
                    if real_mode
                    else "hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442"
                )
                return httpx.Response(
                    200,
                    json={
                        "database": True,
                        "redis": True,
                        "index_ok": True,
                        "calibrated": True,
                        "embedding_fingerprint": fp,
                        "embedding_index": "none" if real_mode else "hnsw",
                        "llm_provider": "openai_compatible" if real_mode else "fake",
                    },
                )
            if path == "/api/session" and request.method == "POST":
                if request.headers.get("origin") != "http://127.0.0.1:43123":
                    return httpx.Response(403, json={"error": {"code": "forbidden"}})
                body = json.loads(request.content.decode("utf-8"))
                email = str(body["email"])
                return httpx.Response(
                    200,
                    headers={"set-cookie": f"la_session={email}; Path=/; HttpOnly"},
                    json={"user": {"id": "u1", "display_name": "测试", "role": "member"}},
                )
            cookie = request.headers.get("cookie") or ""
            if request.method not in ("GET", "HEAD") and path.startswith("/api/backend/"):
                if request.headers.get("origin") != "http://127.0.0.1:43123":
                    return httpx.Response(403, json={"error": {"code": "forbidden"}})
            if path == "/api/backend/documents" and request.method == "POST":
                if "viewer@lab.local" in cookie:
                    return httpx.Response(403, json={"error": {"code": "forbidden"}})
                parsed_form = urllib.parse.parse_qs(request.content.decode("utf-8"))
                title = parsed_form.get("title", ["doc"])[0]
                doc_id = "doc-uploaded-1"
                uploaded_docs[doc_id] = {"id": doc_id, "title": title, "status": "ready"}
                return httpx.Response(202, json={"id": doc_id, "status": "pending"})
            if path == "/api/backend/groups" and request.method == "GET":
                return httpx.Response(
                    200,
                    json={"groups": [{"slug": "helios", "name": "项目赫利俄斯组"}]},
                )
            if path == "/api/backend/documents" and request.method == "GET":
                docs = list(uploaded_docs.values())
                if "admin@lab.local" in cookie or "member@lab.local" in cookie:
                    docs.append(
                        {
                            "id": "helios-doc-id",
                            "title": "项目赫利俄斯",
                            "doc_key": "project-helios",
                            "status": "ready",
                        }
                    )
                return httpx.Response(200, json={"documents": docs})
            if path == "/api/backend/documents/helios-doc-id" and request.method == "GET":
                if "zhou@lab.local" in cookie:
                    return httpx.Response(404, json={"error": {"code": "not_found"}})
                return httpx.Response(200, json={"id": "helios-doc-id", "title": "项目赫利俄斯"})
            if path.startswith("/api/backend/documents/") and request.method == "DELETE":
                doc_id = path.rsplit("/", 1)[-1]
                uploaded_docs.pop(doc_id, None)
                return httpx.Response(200, json={"status": "deleted"})
            if path == "/api/backend/chat/stream" and request.method == "POST":
                msg = str(json.loads(request.content.decode("utf-8")).get("message") or "")
                if "火星基地" in msg:
                    sse = (
                        'event: refusal\ndata: {"type": "refusal", "reason": "low_score"}\n\n'
                        'event: done\ndata: {"type": "done", "refused": true}\n\n'
                    )
                else:
                    sse = (
                        'event: citations\ndata: {"type": "citations", "items": [{"document_id": "doc-uploaded-1", "index": 1}]}\n\n'
                        'event: token\ndata: {"type": "token", "text": "门禁每 17 天轮换一次 [1]。"}\n\n'
                        'event: citation_check\ndata: {"type": "citation_check", "used": [1], "invalid": [], "uncited": false}\n\n'
                        'event: done\ndata: {"type": "done", "refused": false}\n\n'
                    )
                return httpx.Response(200, text=sse)
            if path == "/api/backend/search" and request.method == "POST":
                return httpx.Response(200, json={"hits": []})
            if path == "/api/backend/audit" and request.method == "GET":
                events = [
                    {"action": "login", "detail": {}},
                    {"action": "document_upload", "detail": {}},
                    {"action": "document_indexed", "detail": {}},
                    {"action": "retrieval", "detail": {}},
                    {"action": "answer_refused", "detail": {}},
                    {"action": "permission_denied", "detail": {}},
                    {"action": "llm_call", "detail": {}},
                ]
                return httpx.Response(200, json={"events": events})
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    code_off, table_off = await run_smoke(
        web_url="http://web:3000",
        api_url="http://127.0.0.1:8000",
        offline=True,
        transport=make_transport(real_mode=False),
    )
    assert code_off == 0, table_off
    assert "S9 cleanup" in table_off

    code_real, table_real = await run_smoke(
        web_url="http://web:3000",
        api_url="http://127.0.0.1:8000",
        offline=False,
        transport=make_transport(real_mode=True),
    )
    assert code_real == 0, table_real
    assert "S9 cleanup" in table_real


@pytest.mark.parametrize(
    ("candidate", "email", "valid"),
    [
        ("StrongPass-2026!", "alice@qingheng.lab", True),
        ("Another#Valid99", "bob@qingheng.lab", True),
        ("UpperLower1234", "carol@qingheng.lab", True),
        ("lower!12345678", "dave@qingheng.lab", True),
        ("UPPER!12345678", "erin@qingheng.lab", True),
        ("Short1!Aa", "alice@qingheng.lab", False),
        ("LabAgent-Demo-" + "2026-Admin", "alice@qingheng.lab", False),
        ("LabAgent-Demo-" + "2026-Custom", "alice@qingheng.lab", False),
        ("labagent-demo-" + "2026-member", "alice@qingheng.lab", False),
        ("My-alice-Pass99!", "alice@qingheng.lab", False),
        ("ALICE_Secure99!", "alice@qingheng.lab", False),
        ("alllowercase1234", "alice@qingheng.lab", False),
        ("ALLUPPERCASE1234", "alice@qingheng.lab", False),
        ("OnlyLettersHere", "alice@qingheng.lab", False),
        ("12345678901234", "alice@qingheng.lab", False),
    ],
)
def test_validate_password_policy_table(candidate: str, email: str, valid: bool) -> None:
    from app.auth import validate_password

    if valid:
        validate_password(candidate, email)
    else:
        with pytest.raises(ValueError):
            validate_password(candidate, email)




