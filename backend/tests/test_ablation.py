"""Tests for Qwen3-Embedding-8B ablation automation (Spec T3.2 / PR-22) and p95 latency metrics."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from app.config import get_settings, repo_root, reset_settings_cache
from evals.metrics import _percentile, evaluate
from evals.run_eval import _chunks_table_bytes, _latency_summary, _round_metrics, _run


def _parse_group_blocks(stdout: str) -> dict[str, dict[str, str]]:
    """Parse Group E* blocks from run_embedding_ablation.sh --dry-run output."""
    groups: dict[str, dict[str, str]] = {}
    current_group: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if line.startswith("Group E") and ":" in line:
            header = line.split(":", 1)[0]
            current_group = header.replace("Group ", "").strip()
            groups[current_group] = {}
        elif current_group is not None and "=" in line and not line.startswith("Commands"):
            key, val = line.split("=", 1)
            if key.isupper() and "_" in key or key == "EMBEDDER":
                groups[current_group][key] = val
    return groups


def _make_mock_bin(tmp_path: Path, *, include_q8: bool = True) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    curl_script = bin_dir / "curl"
    curl_script.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == *":9/"* ]]; then\n'
        "    exit 7\n"
        "  fi\n"
        "done\n"
        'printf \'{"object":"list","data":[]}\\n\'\n',
        encoding="utf-8",
    )
    curl_script.chmod(curl_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    q8_line = (
        "qwen3-embedding:8b-q8_0    9704fd987c12    8.0 GB    2 minutes ago\n"
        if include_q8
        else ""
    )
    ollama_script = bin_dir / "ollama"
    ollama_script.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-}" == "list" ]]; then\n'
        "  cat <<'EOF'\n"
        "NAME                       ID              SIZE      MODIFIED\n"
        f"{q8_line}"
        "qwen3-embedding:8b         64b933495768    4.7 GB    24 minutes ago\n"
        "EOF\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    ollama_script.chmod(ollama_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_latency_ms_p95_calculation_and_rounding() -> None:
    assert _percentile([], 0.95) == 0.0
    assert _percentile([12.5], 0.95) == pytest.approx(12.5)
    vals = [float(i) for i in range(1, 22)]  # 21 items: 1.0 .. 21.0
    assert _percentile(vals, 0.50) == pytest.approx(11.0)
    assert _percentile(vals, 0.95) == pytest.approx(20.0)

    rows: list[dict[str, Any]] = []
    for idx, elapsed in enumerate([10.0, 20.0, 30.0, 40.0, 100.0], start=1):
        rows.append(
            {
                "id": f"q{idx}",
                "split": "test",
                "type": "direct",
                "answerable": True,
                "gold": ["doc1#0"],
                "query_tokens": ["门禁"],
                "elapsed_ms": elapsed,
                "df": {"门禁": 1},
                "n_docs": 5,
                "hits": [
                    {"key": "doc1#0", "cosine": 0.85, "bm25": 2.0, "tokens": ["门禁"]},
                ],
            }
        )
    params = {
        "rrf_k": 60,
        "evidence_score_min": 0.5,
        "rare_coverage_min": 0.0,
        "cite_margin": 0.15,
        "cite_k": 2,
    }
    metrics = evaluate(rows, mode="dense", params=params, include_ci=False)
    assert metrics["latency_ms_mean"] == pytest.approx(40.0)
    assert metrics["latency_ms_p50"] == pytest.approx(30.0)
    # 5 items sorted: [10, 20, 30, 40, 100]; pos = 0.95 * 4 = 3.8 -> 40*0.2 + 100*0.8 = 88.0
    assert metrics["latency_ms_p95"] == pytest.approx(88.0)

    rounded = _round_metrics(metrics)
    assert "latency_ms_p95" in rounded
    assert rounded["latency_ms_mean"] == pytest.approx(40.0)
    assert rounded["latency_ms_p50"] == pytest.approx(30.0)
    assert rounded["latency_ms_p95"] == pytest.approx(88.0)

    summary = _latency_summary([10.0, 20.0, 30.0, 40.0, 100.0])
    assert summary == {"mean": 40.0, "p50": 30.0, "p95": 88.0}


async def test_run_eval_records_table_size_and_latency_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CALIBRATION_DIR", str(tmp_path))
    monkeypatch.setenv("EVAL_REINDEX_DURATION_S", "1.2345")
    monkeypatch.setattr("evals.run_eval.MODES", ("dense",))
    monkeypatch.setattr("evals.metrics._thresholds", lambda _scores: [0.0, 0.25])
    reset_settings_cache()
    try:
        await _run(tune_dev=True, check=False, commit=False)
        s = get_settings()
        res_file = tmp_path / f"results-{s.embedding_slug}.json"
        assert res_file.is_file()
        data = json.loads(res_file.read_text(encoding="utf-8"))
        assert isinstance(data.get("chunks_table_bytes"), int)
        assert data["chunks_table_bytes"] > 0
        assert data.get("reindex_duration_s") == pytest.approx(1.2345)

        q_lat = data.get("query_embedding_latency_ms")
        assert isinstance(q_lat, dict)
        assert set(q_lat.keys()) == {"mean", "p50", "p95"}
        assert 0.0 <= q_lat["p50"] <= q_lat["p95"]

        s_lat = data.get("search_latency_ms")
        assert isinstance(s_lat, dict)
        assert set(s_lat.keys()) == {"mean", "p50", "p95"}

        dense_test = data["modes"]["dense"]["test"]
        assert "latency_ms_mean" in dense_test
        assert "latency_ms_p50" in dense_test
        assert "latency_ms_p95" in dense_test

        direct_bytes = await _chunks_table_bytes()
        assert direct_bytes > 0
    finally:
        monkeypatch.delenv("CALIBRATION_DIR", raising=False)
        monkeypatch.delenv("EVAL_REINDEX_DURATION_S", raising=False)
        reset_settings_cache()


def test_ablation_script_dry_run_all_groups(tmp_path: Path) -> None:
    script = repo_root() / "scripts" / "run_embedding_ablation.sh"
    assert script.is_file()
    bin_dir = _make_mock_bin(tmp_path, include_q8=True)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["EMBEDDING_BASE_URL"] = "http://127.0.0.1:11434/v1"

    proc = subprocess.run(
        [str(script), "--dry-run", "--all"],
        cwd=repo_root(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    groups = _parse_group_blocks(proc.stdout)
    assert list(groups.keys()) == ["E1", "E2", "E3", "E4", "E5", "E6", "E7"]

    lab_instr = (
        "Given a question from a research lab member, "
        "retrieve the lab document passage that answers it"
    )
    web_instr = "Given a web search query, retrieve relevant passages that answer the query"

    # E1: MVP default (4096, halfvec, auto -> none, lab instruction)
    assert groups["E1"]["EMBEDDER"] == "openai_compatible"
    assert groups["E1"]["EMBEDDING_MODEL"] == "qwen3-embedding:8b"
    assert groups["E1"]["EMBEDDING_MODEL_VERSION"] == "64b933495768"
    assert groups["E1"]["EMBEDDING_DIM"] == "4096"
    assert groups["E1"]["EMBEDDING_NATIVE_DIM"] == "4096"
    assert groups["E1"]["EMBEDDING_TRUNCATE"] == "false"
    assert groups["E1"]["EMBEDDING_STORAGE"] == "halfvec"
    assert groups["E1"]["EMBEDDING_INDEX"] == "auto"
    assert groups["E1"]["RESOLVED_EMBEDDING_INDEX"] == "none"
    assert groups["E1"]["EMBEDDING_QUERY_INSTRUCTION"] == lab_instr
    assert groups["E1"]["EMBEDDING_DOCUMENT_PREFIX"] == ""
    assert groups["E1"]["EMBEDDING_REDACT"] == "false"
    assert "|4096h|" in groups["E1"]["EMBEDDING_FINGERPRINT"]

    # E2: Generic web search instruction
    assert groups["E2"]["EMBEDDING_QUERY_INSTRUCTION"] == web_instr
    assert groups["E2"]["EMBEDDING_DIM"] == "4096"
    assert groups["E2"]["EMBEDDING_STORAGE"] == "halfvec"

    # E3: No instruction ({query})
    assert groups["E3"]["EMBEDDING_QUERY_TEMPLATE"] == "{query}"
    assert groups["E3"]["EMBEDDING_QUERY_INSTRUCTION"] == ""
    assert "|qp=e3b0c442|" in groups["E3"]["EMBEDDING_FINGERPRINT"]

    # E4: MRL 2048-dim, halfvec, HNSW
    assert groups["E4"]["EMBEDDING_DIM"] == "2048"
    assert groups["E4"]["EMBEDDING_NATIVE_DIM"] == "4096"
    assert groups["E4"]["EMBEDDING_TRUNCATE"] == "true"
    assert groups["E4"]["EMBEDDING_STORAGE"] == "halfvec"
    assert groups["E4"]["EMBEDDING_INDEX"] == "hnsw"
    assert groups["E4"]["RESOLVED_EMBEDDING_INDEX"] == "hnsw"
    assert "|2048h|" in groups["E4"]["EMBEDDING_FINGERPRINT"]

    # E5: MRL 1024-dim, vector, HNSW
    assert groups["E5"]["EMBEDDING_DIM"] == "1024"
    assert groups["E5"]["EMBEDDING_NATIVE_DIM"] == "4096"
    assert groups["E5"]["EMBEDDING_TRUNCATE"] == "true"
    assert groups["E5"]["EMBEDDING_STORAGE"] == "vector"
    assert groups["E5"]["EMBEDDING_INDEX"] == "hnsw"
    assert groups["E5"]["RESOLVED_EMBEDDING_INDEX"] == "hnsw"
    assert "|1024|" in groups["E5"]["EMBEDDING_FINGERPRINT"]

    # E6: 4096-dim, vector fp32, exact search
    assert groups["E6"]["EMBEDDING_DIM"] == "4096"
    assert groups["E6"]["EMBEDDING_TRUNCATE"] == "false"
    assert groups["E6"]["EMBEDDING_STORAGE"] == "vector"
    assert groups["E6"]["EMBEDDING_INDEX"] == "auto"
    assert groups["E6"]["RESOLVED_EMBEDDING_INDEX"] == "none"
    assert "|4096|" in groups["E6"]["EMBEDDING_FINGERPRINT"]

    # E7: q8_0 tag
    assert groups["E7"]["EMBEDDING_MODEL"] == "qwen3-embedding:8b-q8_0"
    assert groups["E7"]["EMBEDDING_MODEL_VERSION"] == "9704fd987c12"
    assert groups["E7"]["EMBEDDING_DIM"] == "4096"
    assert groups["E7"]["EMBEDDING_STORAGE"] == "halfvec"
    assert groups["E7"]["RESOLVED_EMBEDDING_INDEX"] == "none"

    # Every ablation group must produce a unique fingerprint
    fingerprints = [groups[g]["EMBEDDING_FINGERPRINT"] for g in groups]
    assert len(set(fingerprints)) == 7

    assert "python -m app.reindex --apply" in proc.stdout
    assert "python -m evals.run_eval --tune --commit" in proc.stdout
    assert "python -m evals.report" in proc.stdout
    assert "python -m evals.report --check" in proc.stdout
    assert "Pre-populating evaluation corpus" not in proc.stdout


def test_ablation_script_subset_and_optional_e7_skip(tmp_path: Path) -> None:
    script = repo_root() / "scripts" / "run_embedding_ablation.sh"
    bin_dir = _make_mock_bin(tmp_path, include_q8=False)
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["EMBEDDING_BASE_URL"] = "http://127.0.0.1:11434/v1"

    # Subset selection (E1, E5, E7) when q8_0 is absent -> E1 and E5 run, E7 is skipped
    proc = subprocess.run(
        [str(script), "--dry-run", "e1", "E5", "E1", "E7"],
        cwd=repo_root(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    groups = _parse_group_blocks(proc.stdout)
    assert list(groups.keys()) == ["E1", "E5"]
    assert "Skipping optional group E7" in proc.stdout

    # Unknown group argument must fail with exit code 1
    bad_proc = subprocess.run(
        [str(script), "--dry-run", "E9"],
        cwd=repo_root(),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert bad_proc.returncode != 0
    assert "Unknown argument or ablation group" in bad_proc.stderr

    # Unreachable Ollama endpoint must fail pre-check
    bad_env = dict(env)
    bad_env["EMBEDDING_BASE_URL"] = "http://127.0.0.1:9/v1"
    unreachable_proc = subprocess.run(
        [str(script), "--dry-run", "E1"],
        cwd=repo_root(),
        env=bad_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert unreachable_proc.returncode != 0
    assert "Ollama is not reachable" in unreachable_proc.stderr
