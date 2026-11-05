"""Tests for ACL leakage evaluation dataset and runner (Spec T3.3 / PR-23)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app.config import get_settings, repo_root, reset_settings_cache
from app.rag.retrieve import SearchBundle
from evals.run_acl import (
    _run,
    evaluate_case_leaks,
    load_acl_cases,
    summarize_acl_results,
    valid_doc_keys,
)


def test_acl_cases_schema_and_coverage(tmp_path: Path) -> None:
    allowed_keys = valid_doc_keys()
    assert "private-review" in allowed_keys
    assert "project-helios" in allowed_keys
    assert "project-lumen" in allowed_keys
    assert "procurement" in allowed_keys

    cases = load_acl_cases(allowed_doc_keys=allowed_keys)
    assert len(cases) >= 30

    roles_seen = {c["user_role"] for c in cases}
    assert {"member", "viewer"} <= roles_seen

    categories_seen = {c.get("category") for c in cases}
    assert {
        "private_non_owner",
        "restricted_no_grant",
        "restricted_group_unmatched",
        "restricted_group_mixed",
        "restricted_role_mismatch",
        "restricted_other_user",
    } <= categories_seen

    # Verify mixed group case exists where user is in helios and project-lumen is forbidden
    mixed_helios = [
        c
        for c in cases
        if "helios" in c["user_groups"] and "project-lumen" in c["forbidden_doc_keys"]
    ]
    assert len(mixed_helios) >= 1

    # Verify invalid schema is rejected by load_acl_cases
    bad_file = tmp_path / "bad_acl_cases.jsonl"
    bad_file.write_text(
        json.dumps(
            {
                "id": "bad-01",
                "user_role": "member",
                "user_groups": [],
                "grants": {},
                "question": "测试问题",
                "forbidden_doc_keys": ["non-existent-doc-key"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown forbidden_doc_keys"):
        load_acl_cases(path=bad_file, allowed_doc_keys=allowed_keys)


def test_evaluate_case_leaks_and_summary_counts() -> None:
    case = {
        "id": "acl-test-1",
        "category": "restricted_group_mixed",
        "user_role": "member",
        "user_groups": ["helios"],
        "grants": {
            "project-helios": {"visibility": "restricted", "groups": ["helios"]},
            "project-lumen": {"visibility": "restricted", "groups": ["lumen"]},
        },
        "question": "流明和赫利俄斯项目的代码仓库分别是什么？",
        "forbidden_doc_keys": ["project-lumen", "private-review"],
    }
    doc_id_map = {
        "doc-uuid-helios": "project-helios",
        "doc-uuid-lumen": "project-lumen",
        "doc-uuid-private": "private-review",
    }

    # 1. Clean case (0 leaks)
    clean_eval = evaluate_case_leaks(
        case,
        retrieved_hits=[{"chunk_id": "c1", "doc_key": "project-helios"}],
        agent_citations=[{"chunk_id": "c1", "document_id": "doc-uuid-helios"}],
        doc_id_to_key=doc_id_map,
    )
    assert clean_eval["leaked"] is False
    assert clean_eval["retrieval_leaks"] == 0
    assert clean_eval["citation_leaks"] == 0

    # 2. Retrieval leak + citation leak
    leaked_eval = evaluate_case_leaks(
        case,
        retrieved_hits=[
            {"chunk_id": "c1", "doc_key": "project-helios"},
            {"chunk_id": "c2", "doc_key": "project-lumen"},
            {"chunk_id": "c3", "document_id": "doc-uuid-private"},
        ],
        agent_citations=[
            {"chunk_id": "c1", "document_id": "doc-uuid-helios"},
            {"chunk_id": "c2", "document_id": "doc-uuid-lumen"},
        ],
        doc_id_to_key=doc_id_map,
    )
    assert leaked_eval["leaked"] is True
    assert leaked_eval["retrieval_leaks"] == 2
    assert leaked_eval["citation_leaks"] == 1
    assert leaked_eval["leaked_retrieval_keys"] == ["private-review", "project-lumen"]
    assert leaked_eval["leaked_citation_keys"] == ["project-lumen"]

    summary = summarize_acl_results([clean_eval, leaked_eval])
    assert summary["total_cases"] == 2
    assert summary["leaked_cases"] == 1
    assert summary["retrieval_leaks"] == 2
    assert summary["citation_leaks"] == 1
    assert summary["by_category"]["restricted_group_mixed"]["leaked_cases"] == 1
    assert summary["by_role"]["member"]["leaked_cases"] == 1


async def test_run_acl_end_to_end_zero_leaks_and_leak_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBRATION_DIR", str(tmp_path))
    reset_settings_cache()
    try:
        report = await _run(commit=False, check=True)
        assert report["total_cases"] >= 30
        assert report["leaked_cases"] == 0
        assert report["retrieval_leaks"] == 0
        assert report["citation_leaks"] == 0

        s = get_settings()
        runtime_report_path = tmp_path / f"acl-{s.embedding_slug}.json"
        assert runtime_report_path.is_file()
        committed_report_path = repo_root() / "evals" / "results" / "acl" / f"{s.embedding_slug}.json"
        assert committed_report_path.is_file()

        # Verify positive leak detection in _run: if search_knowledge or run_agent leaks a
        # forbidden document, _run counts the leak in the output report and exits non-zero.
        cases = load_acl_cases(allowed_doc_keys=valid_doc_keys())

        async def _leaking_search_knowledge(*_args: Any, **_kwargs: Any) -> SearchBundle:
            return SearchBundle(
                hits=[
                    {
                        "chunk_id": "leaked-chunk-0",
                        "document_id": "leaked-doc-0",
                        "doc_key": "private-review",
                        "chunk_index": 0,
                        "heading": "赫利俄斯批注",
                        "title": "导师私下批注",
                        "collection": "knowledge",
                        "content": "仅导师可见批注-HX17-负例不足",
                        "tokens": ["批注"],
                        "cosine": 0.95,
                        "key": "chunk-0",
                    }
                ],
                coverage=1.0,
                df={"批注": 1},
                n_docs=1,
                elapsed_ms=1.0,
                mode="dense",
            )

        async def _leaking_run_agent(**_kwargs: Any) -> dict[str, object]:
            return {
                "answer": "仅导师可见批注 [1]",
                "refused": False,
                "decision": {"refused": False},
                "citations": [
                    {
                        "index": 1,
                        "chunk_id": "leaked-chunk-0",
                        "document_id": "leaked-doc-0",
                        "doc_key": "private-review",
                        "title": "导师私下批注",
                    }
                ],
                "usage": {},
                "redactions": 0,
                "trace": [],
            }

        monkeypatch.setattr("evals.run_acl.search_knowledge", _leaking_search_knowledge)
        monkeypatch.setattr("evals.run_acl.run_agent", _leaking_run_agent)

        with pytest.raises(SystemExit, match="ACL leakage detected"):
            await _run(commit=False, check=False, cases=cases)

        leaked_report = json.loads(runtime_report_path.read_text(encoding="utf-8"))
        assert leaked_report["leaked_cases"] > 0
        assert leaked_report["retrieval_leaks"] > 0
        assert leaked_report["citation_leaks"] > 0
    finally:
        monkeypatch.delenv("CALIBRATION_DIR", raising=False)
        reset_settings_cache()
