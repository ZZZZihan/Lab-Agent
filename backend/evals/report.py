"""Generate evaluation summary tables in evals/results/README.md and root README.md."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from app.config import repo_root

BEGIN_MARKER = "<!-- eval:begin -->"
END_MARKER = "<!-- eval:end -->"

MODE_LABELS: dict[str, str] = {
    "dense": "dense",
    "hybrid_rrf": "hybrid RRF",
    "hybrid_lexical_rerank": "lexical rerank, pool 40",
    "hybrid_lexical_rerank15": "lexical rerank, pool 15",
    "hybrid_rrf_rerank": "hybrid RRF + API rerank",
}


def _escape_md_cell(text: str) -> str:
    return text.replace("|", r"\|")


def _fmt_metric(val: Any) -> str:
    if val is None:
        return "—"
    if isinstance(val, dict) and "value" in val:
        v = float(val["value"])
        if "ci_low" in val and "ci_high" in val:
            lo = float(val["ci_low"])
            hi = float(val["ci_high"])
            return f"{v:.3f} [{lo:.3f}, {hi:.3f}]"
        return f"{v:.3f}"
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return f"{float(val):.3f}"
    return str(val)


def _extract_date(raw: Any) -> str:
    if not raw or not isinstance(raw, str):
        return "unknown"
    text = raw.strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return text


def _load_result_records(
    results_dir: Path, calibrations_dir: Path
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not results_dir.exists():
        return records
    for path in sorted(results_dir.rglob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        if "embedder_fingerprint" not in doc or "modes" not in doc:
            continue
        inferred_dataset = str(
            doc.get("dataset")
            or (path.parent.name if path.parent != results_dir else "v1")
        )
        cal_doc: dict[str, Any] = {}
        for cal_path in (
            calibrations_dir / inferred_dataset / path.name,
            calibrations_dir / path.name,
        ):
            if cal_path.is_file():
                try:
                    loaded_cal = json.loads(cal_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_cal, dict):
                        cal_doc = loaded_cal
                        break
                except Exception:
                    pass
        dataset = str(
            doc.get("dataset")
            or cal_doc.get("dataset")
            or inferred_dataset
        )
        raw_date = (
            doc.get("generated_at")
            or doc.get("tuned_at")
            or cal_doc.get("tuned_at")
            or "unknown"
        )
        git_sha = str(doc.get("git_sha") or cal_doc.get("git_sha") or "unknown")
        records.append(
            {
                "path": path,
                "slug": path.stem,
                "doc": doc,
                "dataset": dataset,
                "date": _extract_date(raw_date),
                "git_sha": git_sha,
            }
        )
    return records


def _load_e2e_records(results_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    e2e_dir = results_dir / "e2e"
    if not e2e_dir.exists():
        return records
    for path in sorted(e2e_dir.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        if doc.get("suite") != "e2e" and "llm_model" not in doc:
            continue
        metrics = doc.get("metrics")
        if not isinstance(metrics, dict):
            continue
        raw_date = doc.get("generated_at") or doc.get("date") or "unknown"
        records.append(
            {
                "path": path,
                "slug": path.stem,
                "doc": doc,
                "dataset": str(doc.get("dataset") or "v1"),
                "split": str(doc.get("split") or "test"),
                "date": _extract_date(raw_date),
                "git_sha": str(doc.get("git_sha") or "unknown"),
            }
        )
    return records


def render_eval_block(results_dir: Path, calibrations_dir: Path) -> str:
    records = _load_result_records(results_dir, calibrations_dir)
    lines: list[str] = []
    if not records:
        lines.append("_No committed evaluation results found under `evals/results/`._")
        return "\n".join(lines)

    # 1. Run metadata + mode comparison table
    lines.append(
        "| Fingerprint | Dataset | Mode | Split | Questions | Recall@5 | MRR | Citation F1 | Refusal | False refusal | False answer | Date | Git SHA |"
    )
    lines.append(
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"
    )

    type_rows: list[str] = []

    for rec in records:
        doc = rec["doc"]
        fp = _escape_md_cell(str(doc["embedder_fingerprint"]))
        dataset = _escape_md_cell(str(rec["dataset"]))
        date_str = _escape_md_cell(str(rec["date"]))
        git_sha = _escape_md_cell(str(rec["git_sha"]))
        selected_mode = str(doc.get("selected_mode") or "dense")
        modes = doc.get("modes") or {}
        questions_meta = doc.get("questions") or {}

        mode_splits: list[tuple[str, str]] = [
            ("dense", "dev"),
            ("dense", "test"),
            ("hybrid_rrf", "dev"),
            ("hybrid_rrf", "test"),
            ("hybrid_lexical_rerank", "test"),
            ("hybrid_lexical_rerank15", "test"),
        ]
        for mode_key, split in mode_splits:
            mode_entry = modes.get(mode_key)
            if not isinstance(mode_entry, dict):
                continue
            split_metrics = mode_entry.get(split)
            if not isinstance(split_metrics, dict):
                continue
            base_label = MODE_LABELS.get(mode_key, mode_key)
            mode_label = (
                f"{base_label} (selected)" if mode_key == selected_mode else base_label
            )
            raw_n = split_metrics.get(
                "n",
                questions_meta.get(split, 0) if isinstance(questions_meta, dict) else 0,
            )
            n_q = int(round(float(raw_n or 0)))
            lines.append(
                f"| `{fp}` | `{dataset}` | {mode_label} | {split} | {n_q} "
                f"| {_fmt_metric(split_metrics.get('recall_at_5'))} "
                f"| {_fmt_metric(split_metrics.get('mrr'))} "
                f"| {_fmt_metric(split_metrics.get('citation_f1'))} "
                f"| {_fmt_metric(split_metrics.get('refusal_correctness'))} "
                f"| {_fmt_metric(split_metrics.get('false_refusal_rate'))} "
                f"| {_fmt_metric(split_metrics.get('false_answer_rate'))} "
                f"| {date_str} | `{git_sha}` |"
            )

        selected_entry = modes.get(selected_mode)
        if isinstance(selected_entry, dict):
            for split in ("dev", "test"):
                split_metrics = selected_entry.get(split)
                if not isinstance(split_metrics, dict):
                    continue
                by_type = split_metrics.get("by_type")
                if not isinstance(by_type, dict):
                    continue
                for qtype, tmetrics in by_type.items():
                    if not isinstance(tmetrics, dict):
                        continue
                    t_n = int(tmetrics.get("n", 0))
                    t_r5 = _fmt_metric(tmetrics.get("recall_at_5"))
                    t_ref = _fmt_metric(tmetrics.get("refusal_correctness"))
                    type_rows.append(
                        f"| `{fp}` | `{dataset}` | {split} | `{qtype}` | {t_n} | {t_r5} | {t_ref} | {date_str} | `{git_sha}` |"
                    )

    if type_rows:
        lines.append("")
        lines.append("### Breakdown by Question Type (Selected Mode)")
        lines.append("")
        lines.append(
            "| Fingerprint | Dataset | Split | Type | Questions | Recall@5 | Refusal | Date | Git SHA |"
        )
        lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | --- | --- |")
        lines.extend(type_rows)

    e2e_records = _load_e2e_records(results_dir)
    if e2e_records:
        show_judge_col = any(
            isinstance(r["doc"].get("judge"), dict)
            and r["doc"]["judge"].get("human_reviewed") is True
            and r["doc"]["judge"].get("human_agreement") is not None
            for r in e2e_records
        )
        lines.append("")
        lines.append("### End-to-End Generation Evaluation")
        lines.append("")
        header = (
            "| LLM Model | Returned Model | Embedding | Dataset | Split | Questions "
            "| Refusal | False Refusal | False Answer | Citation P | Citation R | Citation F1 "
            "| Invalid Cite | Unannotated | Fact Recall | TTFT p50/p95 (ms) | Latency p50/p95 (ms) | Tokens |"
        )
        sep = (
            "| --- | --- | --- | --- | --- | ---: "
            "| ---: | ---: | ---: | ---: | ---: | ---: "
            "| ---: | ---: | ---: | ---: | ---: | ---: |"
        )
        if show_judge_col:
            header += " Judge Faithfulness (Agreement) |"
            sep += " ---: |"
        header += " Date | Git SHA |"
        sep += " --- | --- |"
        lines.append(header)
        lines.append(sep)

        for rec in e2e_records:
            doc = rec["doc"]
            metrics = doc["metrics"]
            llm_model = _escape_md_cell(str(doc.get("llm_model") or "unknown"))
            ret_ids = doc.get("returned_model_ids") or []
            ret_str = (
                _escape_md_cell(", ".join(str(x) for x in ret_ids))
                if isinstance(ret_ids, list) and ret_ids
                else llm_model
            )
            fp = _escape_md_cell(str(doc.get("embedding_fingerprint") or "unknown"))
            ds = _escape_md_cell(str(rec["dataset"]))
            sp = _escape_md_cell(str(rec["split"]))
            n_q = int(doc.get("n_questions") or 0)
            ttft = metrics.get("ttft_ms") or {}
            tlat = metrics.get("total_latency_ms") or {}
            usage = metrics.get("usage") or {}
            ttft_cell = f"{float(ttft.get('p50', 0.0)):.0f} / {float(ttft.get('p95', 0.0)):.0f}"
            tlat_cell = f"{float(tlat.get('p50', 0.0)):.0f} / {float(tlat.get('p95', 0.0)):.0f}"
            tok_total = int(usage.get("total_tokens") or 0)

            row_str = (
                f"| `{llm_model}` | `{ret_str}` | `{fp}` | `{ds}` | {sp} | {n_q} "
                f"| {_fmt_metric(metrics.get('e2e_refusal_correctness'))} "
                f"| {_fmt_metric(metrics.get('false_refusal_rate'))} "
                f"| {_fmt_metric(metrics.get('false_answer_rate'))} "
                f"| {_fmt_metric(metrics.get('citation_precision'))} "
                f"| {_fmt_metric(metrics.get('citation_recall'))} "
                f"| {_fmt_metric(metrics.get('citation_f1'))} "
                f"| {_fmt_metric(metrics.get('invalid_citation_rate'))} "
                f"| {_fmt_metric(metrics.get('unannotated_rate'))} "
                f"| {_fmt_metric(metrics.get('answer_fact_recall'))} "
                f"| {ttft_cell} | {tlat_cell} | {tok_total} |"
            )
            if show_judge_col:
                judge_info = doc.get("judge")
                if (
                    isinstance(judge_info, dict)
                    and judge_info.get("human_reviewed") is True
                    and judge_info.get("human_agreement") is not None
                ):
                    f_score = float(judge_info.get("faithfulness_score") or 0.0)
                    h_agree = float(judge_info.get("human_agreement") or 0.0)
                    row_str += f" {f_score:.3f} ({h_agree:.2f}) |"
                else:
                    row_str += " — |"
            row_str += f" {_escape_md_cell(str(rec['date']))} | `{_escape_md_cell(str(rec['git_sha']))}` |"
            lines.append(row_str)

    return "\n".join(lines)


def render_results_readme(results_dir: Path, calibrations_dir: Path) -> str:
    block = render_eval_block(results_dir, calibrations_dir)
    return (
        "# Evaluation Results Summary\n\n"
        "_Auto-generated by `python -m evals.report`. Do not edit by hand._\n\n"
        f"{block}\n"
    )


def update_root_readme_text(readme_text: str, block: str) -> str:
    begin_idx = readme_text.find(BEGIN_MARKER)
    end_idx = readme_text.find(END_MARKER)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        raise RuntimeError(
            f"Root README.md is missing {BEGIN_MARKER} ... {END_MARKER} markers"
        )
    before = readme_text[: begin_idx + len(BEGIN_MARKER)]
    after = readme_text[end_idx:]
    return f"{before}\n{block}\n{after}"


def run_report(
    *,
    root: Path | None = None,
    check: bool = False,
) -> int:
    resolved_root = root or repo_root()
    results_dir = resolved_root / "evals" / "results"
    calibrations_dir = resolved_root / "evals" / "calibrations"
    results_readme_path = results_dir / "README.md"
    root_readme_path = resolved_root / "README.md"

    block = render_eval_block(results_dir, calibrations_dir)
    expected_results_readme = render_results_readme(results_dir, calibrations_dir)

    if not root_readme_path.is_file():
        print(f"Missing root README: {root_readme_path}", file=sys.stderr)
        return 1
    current_root_readme = root_readme_path.read_text(encoding="utf-8")
    try:
        expected_root_readme = update_root_readme_text(current_root_readme, block)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if check:
        if not results_readme_path.is_file():
            print(
                f"Missing generated file: {results_readme_path} (run `python -m evals.report`)",
                file=sys.stderr,
            )
            return 1
        current_results_readme = results_readme_path.read_text(encoding="utf-8")
        if current_results_readme != expected_results_readme:
            print(
                f"Out-of-date {results_readme_path} (run `python -m evals.report`)",
                file=sys.stderr,
            )
            return 1
        if current_root_readme != expected_root_readme:
            print(
                f"Out-of-date {root_readme_path} between {BEGIN_MARKER} and {END_MARKER} (run `python -m evals.report`)",
                file=sys.stderr,
            )
            return 1
        return 0

    results_dir.mkdir(parents=True, exist_ok=True)
    results_readme_path.write_text(expected_results_readme, encoding="utf-8")
    root_readme_path.write_text(expected_root_readme, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or verify evaluation summary tables."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit 1 if evals/results/README.md or README.md would change",
    )
    args = parser.parse_args(argv)
    return run_report(check=args.check)


if __name__ == "__main__":
    sys.exit(main())
