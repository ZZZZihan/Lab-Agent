"""Tests for Spec T3.4 End-to-End Generation Evaluation (PR-24)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import httpx
import pytest
from app.config import Settings, repo_root, reset_settings_cache
from evals.report import BEGIN_MARKER, END_MARKER, run_report
from evals.run_e2e import (
    _extract_sentences,
    _run,
    aggregate_e2e_metrics,
    compute_fact_recall,
    detect_model_vendor,
    normalize_for_fact_match,
    run_judge_on_results,
    score_question_e2e,
    validate_judge_model,
)


def test_questions_jsonl_has_valid_answer_facts() -> None:
    q_path = repo_root() / "evals" / "data" / "v1" / "questions.jsonl"
    rows = [
        json.loads(line)
        for line in q_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(rows) == 78
    answerable_rows = [r for r in rows if r["answerable"]]
    unanswerable_rows = [r for r in rows if not r["answerable"]]
    assert len(answerable_rows) == 60
    assert len(unanswerable_rows) == 18

    for r in answerable_rows:
        facts = r.get("answer_facts")
        assert isinstance(facts, list), f"{r['id']} missing answer_facts list"
        assert 1 <= len(facts) <= 4, f"{r['id']} should have 1..4 facts"
        assert all(isinstance(f, str) and f.strip() for f in facts)

    for r in unanswerable_rows:
        assert r.get("answer_facts") == [], f"{r['id']} unanswerable must have empty answer_facts"


def test_fact_recall_normalization_and_matching() -> None:
    assert normalize_for_fact_match("５０,０００ 元") == normalize_for_fact_match("五万元")
    assert normalize_for_fact_match("12,000 元") == normalize_for_fact_match("一万二千元")
    assert normalize_for_fact_match("两个工作日") == normalize_for_fact_match("2 个工作日")
    assert normalize_for_fact_match("二十四小时内") == normalize_for_fact_match("24 小时内")

    answer = "根据文档，单笔采购超过 50,000 元需提前两周在采购系统中立项并附三家比价单 [1]。"
    facts = ["50000", "两周", "三家"]
    assert compute_fact_recall(answer, facts) == pytest.approx(1.0)

    partial = "单笔超过五万元需要提前立项 [1]。"
    assert compute_fact_recall(partial, facts) == pytest.approx(1.0 / 3.0)

    assert compute_fact_recall(answer, facts, refused=True) == pytest.approx(0.0)
    assert compute_fact_recall("", facts) == pytest.approx(0.0)


def test_citation_mapping_and_e2e_scoring() -> None:
    initial_citations = [
        {
            "index": 1,
            "doc_key": "compute-cluster",
            "chunk_index": 0,
            "title": "计算集群使用手册",
            "heading": "节点规格",
            "excerpt": "清衡-A100 共 4 台节点，每台配备 8 张 A100-SXM4-80GB。",
        },
        {
            "index": 2,
            "doc_key": "compute-cluster",
            "chunk_index": 1,
            "title": "计算集群使用手册",
            "heading": "Slurm 队列与限额",
            "excerpt": "gpu-short 最长 4 小时，单用户最多 2 张卡。",
        },
    ]
    q_row = {
        "id": "q001",
        "split": "test",
        "type": "direct",
        "question": "清衡-A100 节点每台有几张 GPU，显存多大？",
        "answerable": True,
        "answer_facts": ["8张", "80GB"],
    }

    scored = score_question_e2e(
        q_row,
        answer="清衡-A100 每台节点配备 8 张 GPU，显存 80GB [1][9]。",
        refused=False,
        decision_reason="ok",
        initial_citations=initial_citations,
        citation_check_event={"used": [1], "invalid": [9], "uncited": False},
        gold_keys=["compute-cluster#0"],
        ttft_ms=120.5,
        total_latency_ms=250.0,
        usage={"prompt_tokens": 100, "completion_tokens": 30, "model": "gemini-3.8-flash"},
    )
    assert scored["used_citation_keys"] == ["compute-cluster#0"]
    assert scored["citation_precision"] == pytest.approx(1.0)
    assert scored["citation_recall"] == pytest.approx(1.0)
    assert scored["citation_f1"] == pytest.approx(1.0)
    assert scored["has_invalid_citation"] == pytest.approx(1.0)
    assert scored["is_unannotated"] == pytest.approx(0.0)
    assert scored["fact_recall"] == pytest.approx(1.0)
    assert scored["refusal_correct"] == pytest.approx(1.0)
    assert scored["false_refusal"] == pytest.approx(0.0)
    assert scored["false_answer"] is None

    # Unanswerable question where retrieval did not refuse, but LLM declined -> counted as refusal
    unans_row = {
        "id": "q061",
        "split": "test",
        "type": "unanswerable",
        "question": "实验室 H100 节点有几张卡？",
        "answerable": False,
        "answer_facts": [],
    }
    scored_unans = score_question_e2e(
        unans_row,
        answer="无法根据现有文档回答。",
        refused=True,
        decision_reason="model_declined",
        initial_citations=initial_citations,
        citation_check_event={"used": [], "invalid": [], "uncited": False},
        gold_keys=[],
        ttft_ms=80.0,
        total_latency_ms=110.0,
        usage={"prompt_tokens": 90, "completion_tokens": 10, "model": "gemini-3.8-flash"},
    )
    assert scored_unans["refused"] is True
    assert scored_unans["refusal_correct"] == pytest.approx(1.0)
    assert scored_unans["false_answer"] == pytest.approx(0.0)

    agg = aggregate_e2e_metrics([scored, scored_unans])
    assert agg["e2e_refusal_correctness"] == pytest.approx(1.0)
    assert agg["false_refusal_rate"] == pytest.approx(0.0)
    assert agg["false_answer_rate"] == pytest.approx(0.0)
    assert agg["citation_f1"] == pytest.approx(1.0)
    assert agg["invalid_citation_rate"] == pytest.approx(1.0)
    assert agg["unannotated_rate"] == pytest.approx(0.0)
    assert agg["answer_fact_recall"] == pytest.approx(1.0)
    assert agg["usage"]["total_tokens"] == 230
    assert agg["usage"]["returned_models"] == ["gemini-3.8-flash"]


def test_validate_judge_model_enforces_cross_vendor() -> None:
    assert detect_model_vendor("antigravity/gemini-3.8-flash") == "google"
    assert detect_model_vendor("grok/grok-4.7-build-fast") == "xai"
    assert detect_model_vendor("remote-magpie/auto/claude-sonnet-5-5") == "anthropic"
    assert detect_model_vendor("copilot/gpt-4.1") == "openai"

    with pytest.raises(ValueError, match="must be non-empty"):
        validate_judge_model("antigravity/gemini-3.8-flash", "")

    with pytest.raises(ValueError, match="must not be identical"):
        validate_judge_model("antigravity/gemini-3.8-flash", "antigravity/gemini-3.8-flash")

    with pytest.raises(ValueError, match="different vendor family"):
        validate_judge_model("antigravity/gemini-3.8-flash", "google/gemini-2.5-pro")

    with pytest.raises(ValueError, match="different vendor family"):
        validate_judge_model("anthropic/claude-sonnet-5-5", "claude-opus-5-5")

    # Cross-vendor succeeds
    validate_judge_model("antigravity/gemini-3.8-flash", "grok/grok-4.7-build-fast")
    validate_judge_model("grok/grok-4.7-build-fast", "antigravity/gemini-3.8-flash")


async def test_judge_pipeline_and_human_audit_csv_export(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        user_msg = body["messages"][-1]["content"]
        score = "0" if "H200" in user_msg else "1"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": score}}]},
        )

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:3425/v1",
        llm_api_key="magpie-test-key",  # type: ignore[arg-type]
        llm_model="antigravity/gemini-3.8-flash",
        judge_model="grok/grok-4.7-build-fast",
        load_tuned_config=False,
    )

    q_results = [
        {
            "id": "q001",
            "refused": False,
            "answer": "每台节点有 8 张 A100 [1]。另外还有 100 张 H200 [1]。",
            "cite_text_by_index": {
                1: "[1] 《计算集群使用手册》·节点规格: 清衡-A100 共 4 台节点，每台配备 8 张 A100。"
            },
        }
    ]
    csv_path = tmp_path / "e2e" / "run1" / "human_audit.csv"
    judge_meta = await run_judge_on_results(
        settings=settings,
        llm_model="antigravity/gemini-3.8-flash",
        question_results=q_results,
        audit_csv_path=csv_path,
        transport=httpx.MockTransport(handler),
    )

    assert judge_meta["judge_model"] == "grok/grok-4.7-build-fast"
    assert judge_meta["judge_vendor"] == "xai"
    assert judge_meta["n_sentences"] == 2
    assert judge_meta["faithfulness_score"] == pytest.approx(0.5)
    assert judge_meta["human_reviewed"] is False
    assert judge_meta["human_agreement"] is None
    assert csv_path.is_file()

    with csv_path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert list(rows[0].keys()) == [
        "question_id",
        "sentence",
        "cited_evidence",
        "judge_score",
        "human_score",
        "notes",
    ]


def test_report_omits_unreviewed_judge_scores(tmp_path: Path) -> None:
    results_v1 = tmp_path / "evals" / "results" / "v1"
    results_e2e = tmp_path / "evals" / "results" / "e2e"
    calibrations = tmp_path / "evals" / "calibrations"
    results_v1.mkdir(parents=True)
    results_e2e.mkdir(parents=True)
    calibrations.mkdir(parents=True)

    fp = "hashing|hashing-cjk-v1|512|redact=0|qp=e3b0c442|dp=e3b0c442"
    (results_v1 / "hashing.json").write_text(
        json.dumps(
            {
                "dataset": "v1",
                "generated_at": "2026-11-05T00:00:00Z",
                "git_sha": "abc1234",
                "embedder_fingerprint": fp,
                "selected_mode": "dense",
                "questions": {"total": 78, "dev": 51, "test": 27},
                "modes": {
                    "dense": {
                        "test": {
                            "n": 27.0,
                            "recall_at_5": {"value": 0.95, "ci_low": 0.85, "ci_high": 1.0, "n": 21},
                            "mrr": {"value": 0.88, "ci_low": 0.78, "ci_high": 0.97, "n": 21},
                            "citation_f1": {"value": 0.66, "ci_low": 0.47, "ci_high": 0.85, "n": 21},
                            "refusal_correctness": {"value": 0.70, "ci_low": 0.51, "ci_high": 0.85, "n": 27},
                            "false_refusal_rate": {"value": 0.19, "ci_low": 0.04, "ci_high": 0.38, "n": 21},
                            "false_answer_rate": {"value": 0.66, "ci_low": 0.33, "ci_high": 1.0, "n": 6},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    e2e_doc = {
        "suite": "e2e",
        "dataset": "v1",
        "split": "test",
        "n_questions": 27,
        "llm_model": "antigravity/gemini-3.8-flash",
        "returned_model_ids": ["gemini-3.8-flash"],
        "embedding_fingerprint": fp,
        "generated_at": "2026-11-05T00:00:00Z",
        "git_sha": "abc1234",
        "metrics": {
            "e2e_refusal_correctness": 0.852,
            "false_refusal_rate": 0.190,
            "false_answer_rate": 0.0,
            "citation_precision": 0.738,
            "citation_recall": 0.762,
            "citation_f1": 0.746,
            "invalid_citation_rate": 0.0,
            "unannotated_rate": 0.0,
            "answer_fact_recall": 0.762,
            "ttft_ms": {"mean": 2000.0, "p50": 1900.0, "p95": 3100.0},
            "total_latency_ms": {"mean": 2100.0, "p50": 2000.0, "p95": 3200.0},
            "usage": {"total_tokens": 8520},
        },
        "judge": {
            "judge_model": "grok/grok-4.7-build-fast",
            "faithfulness_score": 0.912,
            "human_reviewed": False,
            "human_agreement": None,
        },
    }
    e2e_file = results_e2e / "gemini__hashing.json"
    e2e_file.write_text(json.dumps(e2e_doc), encoding="utf-8")

    root_readme = tmp_path / "README.md"
    root_readme.write_text(
        f"# Title\n\n{BEGIN_MARKER}\nstale\n{END_MARKER}\n",
        encoding="utf-8",
    )
    assert run_report(root=tmp_path, check=False) == 0
    rendered = (tmp_path / "evals" / "results" / "README.md").read_text(encoding="utf-8")
    assert "### End-to-End Generation Evaluation" in rendered
    assert "0.912" not in rendered
    assert "Judge Faithfulness" not in rendered

    # Once human_reviewed is True and human_agreement is provided, Judge column is rendered
    e2e_doc["judge"]["human_reviewed"] = True
    e2e_doc["judge"]["human_agreement"] = 0.87
    e2e_file.write_text(json.dumps(e2e_doc), encoding="utf-8")
    assert run_report(root=tmp_path, check=False) == 0
    rendered_reviewed = (tmp_path / "evals" / "results" / "README.md").read_text(encoding="utf-8")
    assert "Judge Faithfulness (Agreement)" in rendered_reviewed
    assert "0.912 (0.87)" in rendered_reviewed


async def test_run_e2e_offline_smoke_10_questions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EMBEDDER", "hashing")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_MODEL", "fake-grounded-v1")
    monkeypatch.setenv("CALIBRATION_DIR", str(tmp_path / "calibrations"))
    reset_settings_cache()
    try:
        reports = await _run(
            dataset="v1",
            split="test",
            max_questions=10,
            models=["fake-grounded-v1"],
            use_judge=False,
            commit=False,
            check=False,
        )
        assert len(reports) == 1
        rep = reports[0]
        assert rep["suite"] == "e2e"
        assert rep["dataset"] == "v1"
        assert rep["split"] == "test"
        assert rep["n_questions"] == 10
        assert rep["llm_model"] == "fake-grounded-v1"
        assert rep["returned_model_ids"] == ["fake-grounded-v1"]
        metrics = rep["metrics"]
        assert 0.0 <= metrics["e2e_refusal_correctness"] <= 1.0
        assert 0.0 <= metrics["citation_f1"] <= 1.0
        assert 0.0 <= metrics["answer_fact_recall"] <= 1.0
        assert metrics["invalid_citation_rate"] == pytest.approx(0.0)
        assert metrics["usage"]["total_tokens"] > 0
        out_file = (
            tmp_path
            / "calibrations"
            / "e2e"
            / "fake-grounded-v1__hashing-hashing-cjk-v1-512-redact-0-qp-e3b0c442-dp-e3b0c442.json"
        )
        assert out_file.is_file()
    finally:
        reset_settings_cache()


async def test_codex_review_fixes_sentences_redaction_errors_and_seed_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typing import Any

    from app.db import dispose_engine, get_sessionmaker
    from app.models import Document, DocumentACL
    from app.seed import seed
    from sqlalchemy import select

    # 1. Post-punctuation citation tags stay attached to their sentences; no standalone "[2]" fragment
    extracted = _extract_sentences("句一。[1] 句二。[2]")
    assert extracted == ["句一。[1]", "句二。[2]"]
    assert _extract_sentences("句一。[1][2]\n[3]\n句二 [3]。") == [
        "句一。[1][2]",
        "句二 [3]。",
    ]

    # 2. run_judge_on_results redacts PII in outbound judge prompts when settings.llm_redact=True
    captured_prompts: list[str] = []

    def redact_check_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        user_msg = str(body["messages"][-1]["content"])
        captured_prompts.append(user_msg)
        return httpx.Response(200, json={"choices": [{"message": {"content": "1"}}]})

    settings_redact = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        llm_provider="openai_compatible",
        llm_base_url="http://127.0.0.1:3425/v1",
        llm_api_key="magpie-test-key",  # type: ignore[arg-type]
        llm_model="antigravity/gemini-3.8-flash",
        judge_model="grok/grok-4.7-build-fast",
        llm_redact=True,
        load_tuned_config=False,
    )
    sec_email = "sec" + chr(64) + "qingheng.lab"
    phone_num = "138" + "12345678"
    await run_judge_on_results(
        settings=settings_redact,
        llm_model="antigravity/gemini-3.8-flash",
        question_results=[
            {
                "id": "q056",
                "refused": False,
                "answer": f"事故需在 24 小时内上报至 {sec_email} 或致电 {phone_num}。[1]",
                "cite_text_by_index": {
                    1: f"[1] 《计算安全》·事故上报: 写信给 {sec_email}，电话 {phone_num}。"
                },
            }
        ],
        audit_csv_path=tmp_path / "redact_audit.csv",
        transport=httpx.MockTransport(redact_check_handler),
    )
    assert len(captured_prompts) == 1
    assert sec_email not in captured_prompts[0]
    assert phone_num not in captured_prompts[0]
    assert "[REDACTED_EMAIL]" in captured_prompts[0]
    assert "[REDACTED_PHONE]" in captured_prompts[0]

    # 3. score_question_e2e and _run reject agent failures (llm_error / embedding_error)
    with pytest.raises(RuntimeError, match="Agent execution failed on q001: http_503"):
        score_question_e2e(
            {
                "id": "q001",
                "split": "test",
                "type": "direct",
                "question": "测试",
                "answerable": True,
                "answer_facts": ["事实"],
            },
            answer="",
            refused=False,
            decision_reason="ok",
            initial_citations=[],
            citation_check_event=None,
            gold_keys=["doc#0"],
            ttft_ms=None,
            total_latency_ms=10.0,
            usage={},
            llm_error="http_503",
        )

    # 4. Running _run leaves existing source="seed" documents (private-review and seed ACLs) untouched
    monkeypatch.setenv("EMBEDDER", "hashing")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_MODEL", "fake-grounded-v1")
    monkeypatch.setenv("CALIBRATION_DIR", str(tmp_path / "calibrations"))
    reset_settings_cache()

    await seed()
    maker = get_sessionmaker()
    async with maker() as session:
        seed_docs_before = {
            doc.doc_key: (doc.id, doc.owner_id, doc.visibility)
            for doc in await session.scalars(select(Document).where(Document.source == "seed"))
            if doc.doc_key
        }
        assert "private-review" in seed_docs_before
        helios_id = seed_docs_before["project-helios"][0]
        helios_acls_before = [
            (r.principal_type, r.principal_id, r.permission)
            for r in await session.scalars(
                select(DocumentACL).where(DocumentACL.document_id == helios_id)
            )
        ]
        assert len(helios_acls_before) > 0
    await dispose_engine()

    await _run(
        dataset="v1",
        split="test",
        max_questions=2,
        models=["fake-grounded-v1"],
        use_judge=False,
        commit=False,
        check=False,
    )

    maker = get_sessionmaker()
    async with maker() as session:
        seed_docs_after = {
            doc.doc_key: (doc.id, doc.owner_id, doc.visibility)
            for doc in await session.scalars(select(Document).where(Document.source == "seed"))
            if doc.doc_key
        }
        assert seed_docs_after == seed_docs_before
        helios_acls_after = [
            (r.principal_type, r.principal_id, r.permission)
            for r in await session.scalars(
                select(DocumentACL).where(DocumentACL.document_id == helios_id)
            )
        ]
        assert helios_acls_after == helios_acls_before
    await dispose_engine()

    # Also verify _run raises RuntimeError when run_agent returns llm_error
    async def _failing_run_agent(**_kwargs: Any) -> dict[str, object]:
        return {
            "answer": "",
            "refused": False,
            "decision": {"refused": False, "reason": "ok"},
            "citations": [],
            "usage": {},
            "redactions": 0,
            "trace": [],
            "llm_error": "http_502",
            "embedding_error": None,
        }

    monkeypatch.setattr("evals.run_e2e.run_agent", _failing_run_agent)
    with pytest.raises(RuntimeError, match="Agent execution failed"):
        await _run(
            dataset="v1",
            split="test",
            max_questions=1,
            models=["fake-grounded-v1"],
            use_judge=False,
            commit=False,
            check=False,
        )
    reset_settings_cache()


async def test_codex_followup_key_file_promotion_reindex_drain_and_q060_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings
    from app.db import dispose_engine, get_sessionmaker
    from app.models import Chunk, Document
    from app.rag.index_meta import read_meta
    from app.seed import seed
    from evals.run_e2e import _resolve_model_settings
    from evals.run_eval import _load_questions
    from sqlalchemy import func, select, text

    # 1. q060 answer_facts includes the negative room assertion "不在 B-214"
    root = repo_root()
    v1_questions = _load_questions(root / "evals" / "data" / "v1" / "questions.jsonl")
    q060 = next(q for q in v1_questions if q["id"] == "q060")
    assert q060["answer_facts"] == ["883-102-447", "不在 B-214"]

    # 2. Promoting LLM_PROVIDER=fake with LLM_API_KEY_FILE resolves llm_api_key
    secret_file = tmp_path / "llm_api_key.txt"
    secret_file.chmod(0o600) if secret_file.exists() else None
    secret_file.write_text("sk-file-resolved-secret-12345\n", encoding="utf-8")
    secret_file.chmod(0o600)

    base = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url="postgresql+asyncpg://labagent:labagent@localhost/labagent",
        jwt_secret="test-only-jwt-secret-value-0123456789-abcdef",  # type: ignore[arg-type]
        cors_origins="http://localhost:43123",
        llm_provider="fake",
        llm_base_url="http://127.0.0.1:8317/v1",
        llm_api_key_file=str(secret_file),
        load_tuned_config=False,
    )
    assert base.llm_api_key is None
    promoted = _resolve_model_settings(
        base,
        "antigravity/gemini-3.8-flash",
        corpus_dir=root / "evals" / "data" / "v1" / "corpus",
        questions_path=root / "evals" / "data" / "v1" / "questions.jsonl",
    )
    assert promoted.llm_provider == "openai_compatible"
    assert promoted.llm_api_key is not None
    assert promoted.llm_api_key.get_secret_value() == "sk-file-resolved-secret-12345"

    # 3. When apply_reindex rebuilds in _run, process_next_pending drains pre-existing seed docs
    monkeypatch.setenv("EMBEDDER", "hashing")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_MODEL", "fake-grounded-v1")
    monkeypatch.setenv("CALIBRATION_DIR", str(tmp_path / "calibrations"))
    reset_settings_cache()

    await seed()
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(
            text(
                """
                INSERT INTO embedding_index_meta (id, fingerprint, embedder, model, dim, storage, index_kind, updated_at)
                VALUES (1, 'openai_compatible|stale-model|512|redact=0|qp=e3b0c442|dp=e3b0c442', 'openai_compatible', 'stale-model', 512, 'vector', 'hnsw', now())
                ON CONFLICT (id) DO UPDATE SET
                    fingerprint = EXCLUDED.fingerprint,
                    embedder = EXCLUDED.embedder,
                    model = EXCLUDED.model,
                    dim = EXCLUDED.dim,
                    storage = EXCLUDED.storage,
                    index_kind = EXCLUDED.index_kind,
                    updated_at = now()
                """
            )
        )
        await session.commit()
    await dispose_engine()

    expected_fp = get_settings().embedding_fingerprint
    await _run(
        dataset="v1",
        split="test",
        max_questions=2,
        models=["fake-grounded-v1"],
        use_judge=False,
        commit=False,
        check=False,
    )

    maker = get_sessionmaker()
    async with maker() as session:
        meta = await read_meta(session)
        assert meta is not None
        assert meta.fingerprint == expected_fp
        seed_docs = list(
            await session.scalars(select(Document).where(Document.source == "seed"))
        )
        assert len(seed_docs) > 0
        for sdoc in seed_docs:
            assert sdoc.status == "ready"
            chunk_cnt = int(
                (
                    await session.execute(
                        select(func.count()).where(Chunk.document_id == sdoc.id)
                    )
                ).scalar_one()
            )
            assert chunk_cnt > 0
    await dispose_engine()
    reset_settings_cache()


