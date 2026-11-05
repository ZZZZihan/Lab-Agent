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
