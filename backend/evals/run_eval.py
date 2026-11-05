"""Evaluate grounded retrieval. Thresholds are fit on the dev split only."""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from app.auth import UNUSABLE_PASSWORD_HASH
from app.config import get_settings, repo_root
from app.db import dispose_engine, get_sessionmaker
from app.models import User
from app.rag.chunker import chunk_markdown, parse_front_matter
from app.rag.embedder import build_embedder
from app.rag.ingest import replace_source
from app.rag.retrieve import search_knowledge
from app.rag.tokenize import content_tokens
from sqlalchemy import select

from evals.metrics import evaluate, metric_value, tune

MODES = (
    "dense",
    "hybrid_rrf",
    "hybrid_lexical_rerank",
    "hybrid_lexical_rerank15",
)


def _load_questions(path: Path) -> list[dict[str, object]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _gold_map(corpus_dir: Path) -> dict[tuple[str, str], str]:
    mapping: dict[tuple[str, str], str] = {}
    for file in sorted(corpus_dir.glob("*.md")):
        meta, body = parse_front_matter(file.read_text(encoding="utf-8"))
        key = meta["key"]
        seen: set[str] = set()
        for chunk in chunk_markdown(body):
            if chunk.heading in seen:
                raise RuntimeError(f"duplicate heading {key} / {chunk.heading}")
            seen.add(chunk.heading)
            mapping[(key, chunk.heading)] = f"{key}#{chunk.index}"
    return mapping


def _read_corpus(corpus_dir: Path) -> list[tuple[dict[str, str], str]]:
    records = []
    for file in sorted(corpus_dir.glob("*.md")):
        records.append(parse_front_matter(file.read_text(encoding="utf-8")))
    return records


async def _prepare(settings: object) -> User:
    from app.config import Settings

    assert isinstance(settings, Settings)
    embedder = build_embedder(settings)
    maker = get_sessionmaker()
    try:
        async with maker() as session:
            from app.rag.index_meta import column_type
            from app.reindex import apply_reindex

            col_storage, col_dim = await column_type(session)
            if (col_storage, col_dim) != (settings.embedding_storage, settings.embedding_dim):
                await apply_reindex(session, settings, embedder, if_needed=True)
            user = await session.scalar(select(User).where(User.email == "eval@lab.local"))
            if user is None:
                user = User(
                    email="eval@lab.local",
                    display_name="评测",
                    role="admin",
                    password_hash=UNUSABLE_PASSWORD_HASH,
                    is_active=False,
                )
                session.add(user)
                await session.flush()
            else:
                user.password_hash = UNUSABLE_PASSWORD_HASH
                user.is_active = False
                await session.flush()
            count = await replace_source(
                session,
                embedder=embedder,
                owner_id=user.id,
                source="eval",
                records=_read_corpus(Path(settings.corpus_dir)),
            )
            await session.commit()
            print(f"ingested {count} eval documents with embedder={embedder.name}")
            return user
    finally:
        await embedder.aclose()


async def _featurize(settings: object, user: User, questions: list[dict[str, object]]) -> list[dict[str, object]]:
    from app.acl import Principal, load_user_group_ids
    from app.config import Settings

    assert isinstance(settings, Settings)
    embedder = build_embedder(settings)
    gold_map = _gold_map(Path(settings.corpus_dir))
    maker = get_sessionmaker()
    features: list[dict[str, object]] = []
    try:
        async with maker() as session:
            group_ids = await load_user_group_ids(session, user.id)
            principal = Principal(id=user.id, role=user.role, group_ids=group_ids)
            for question in questions:
                bundle = await search_knowledge(
                    session,
                    settings=settings,
                    embedder=embedder,
                    user=principal,
                    query=str(question["question"]),
                    mode="dense",
                    source="eval",
                    limit=None,
                )
                gold = []
                gold_refs = question["gold"]
                if not isinstance(gold_refs, list):
                    raise RuntimeError(f"gold must be a list for {question['id']}")
                for ref in gold_refs:
                    assert isinstance(ref, dict)
                    try:
                        gold.append(gold_map[(str(ref["doc"]), str(ref["heading"]))])
                    except KeyError as exc:
                        raise RuntimeError(f"missing gold heading for {question['id']}: {ref}") from exc
                features.append(
                    {
                        "id": question["id"],
                        "split": question["split"],
                        "type": question["type"],
                        "answerable": bool(question["answerable"]),
                        "gold": gold,
                        "query_tokens": content_tokens(str(question["question"])),
                        "elapsed_ms": bundle.elapsed_ms,
                        "df": bundle.df,
                        "n_docs": bundle.n_docs,
                        "hits": [
                            {
                                "key": hit["key"],
                                "cosine": float(hit.get("cosine") or 0.0),
                                "bm25": float(hit.get("bm25") or 0.0),
                                "tokens": list(hit.get("tokens") or []),
                                "chunk_id": hit["chunk_id"],
                                "document_id": hit["document_id"],
                                "heading": hit.get("heading"),
                                "title": hit.get("title"),
                                "chunk_index": hit.get("chunk_index"),
                            }
                            for hit in bundle.hits
                        ],
                    }
                )
        return features
    finally:
        await embedder.aclose()


def _choose_rrf(rows: list[dict[str, object]], mode: str) -> int:
    """Pick RRF k on the dev split using retrieval metrics only."""
    if mode == "dense":
        return 60
    best_k = 60
    best = (-1.0, -1.0)
    for rrf_k in (20, 40, 60, 90):
        metrics = evaluate(
            rows,  # type: ignore[arg-type]
            mode=mode,
            params={
                "rrf_k": rrf_k,
                "evidence_score_min": 0.0,
                "rare_coverage_min": 0.0,
                "cite_margin": 1.0,
                "cite_k": 4,
            },
            include_ci=False,
        )
        key = (metric_value(metrics["recall_at_5"]), metric_value(metrics["mrr"]))
        if key > best:
            best = key
            best_k = rrf_k
    return best_k


def _round_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = round(float(value), 4)
        elif isinstance(value, dict) and {"value", "ci_low", "ci_high", "n"} <= set(value.keys()):
            out[key] = {
                "value": round(float(value["value"]), 4),
                "ci_low": round(float(value["ci_low"]), 4),
                "ci_high": round(float(value["ci_high"]), 4),
                "n": int(value["n"]),
            }
        elif key == "by_type" and isinstance(value, dict):
            rounded_types: dict[str, Any] = {}
            for qtype, tvals in value.items():
                if isinstance(tvals, dict):
                    rounded_types[qtype] = {
                        "n": int(tvals.get("n", 0)),
                        "n_answerable": int(tvals.get("n_answerable", 0)),
                        "recall_at_5": (
                            round(float(tvals["recall_at_5"]), 4)
                            if tvals.get("recall_at_5") is not None
                            else None
                        ),
                        "refusal_correctness": round(float(tvals.get("refusal_correctness", 0.0)), 4),
                    }
            out[key] = rounded_types
        else:
            out[key] = value
    return out


def _git_sha(root: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
              cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return "unknown"


async def _run(tune_dev: bool, check: bool, commit: bool = False) -> None:
    settings = get_settings()
    root = repo_root()
    slug = settings.embedding_slug
    fingerprint = settings.embedding_fingerprint
    runtime_dir = Path(settings.calibration_dir)
    repo_cal_path = root / "evals" / "calibrations" / f"{slug}.json"
    repo_results_path = root / "evals" / "results" / "v1" / f"{slug}.json"
    runtime_cal_path = runtime_dir / f"{slug}.json"
    runtime_results_path = runtime_dir / f"results-{slug}.json"

    if check and not (tune_dev and commit) and not repo_results_path.is_file():
        print(f"该嵌入器没有已提交的基线: {repo_results_path}", file=sys.stderr)
        raise SystemExit(3)

    questions = _load_questions(Path(settings.questions_path))
    if len(questions) < 60:
        raise SystemExit(f"eval set has {len(questions)} questions, need at least 60")
    user = await _prepare(settings)
    features = await _featurize(settings, user, questions)

    existing: dict[str, object] | None = None
    for candidate in (runtime_cal_path, repo_cal_path):
        if candidate.is_file():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                loaded = None
            if isinstance(loaded, dict) and loaded.get("embedder_fingerprint") == fingerprint:
                existing = loaded
            break
    if existing is None and repo_results_path.is_file():
        committed_doc = json.loads(repo_results_path.read_text(encoding="utf-8"))
        if isinstance(committed_doc, dict) and isinstance(
            committed_doc.get("selected_config"), dict
        ):
            existing = dict(committed_doc["selected_config"])

    mode_reports: dict[str, object] = {}
    selected_mode = "dense"
    selected_params: dict[str, object] = {}
    best_recall = (-1.0, -1.0)
    for mode in MODES:
        dev_rows = [row for row in features if row["split"] == "dev"]
        test_rows = [row for row in features if row["split"] == "test"]
        if tune_dev:
            rrf_k = _choose_rrf(dev_rows, mode)
            tuned = tune(dev_rows, mode, rrf_k)  # type: ignore[arg-type]
            params = tuned["params"]
            dev_metrics = tuned["dev"]
        else:
            if existing is None:
                await dispose_engine()
                raise SystemExit(f"Calibration for {slug} is missing; run with --tune first")
            params = {
                "rrf_k": existing.get("rrf_k", 60) if mode == existing.get("retrieval_mode") else 60,
                "evidence_score_min": existing.get("evidence_score_min", 0.99),
                "rare_coverage_min": existing.get("rare_coverage_min", 0.0),
                "cite_margin": existing.get("cite_margin", 0.15),
                "cite_k": existing.get("cite_k", 3),
            }
            # Non-selected modes keep a documented default so the table still renders.
            if mode != existing.get("retrieval_mode"):
                params = {
                    "rrf_k": 60,
                    "evidence_score_min": existing.get("evidence_score_min", 0.99),
                    "rare_coverage_min": existing.get("rare_coverage_min", 0.0),
                    "cite_margin": existing.get("cite_margin", 1.0),
                    "cite_k": existing.get("cite_k", 4),
                }
            dev_metrics = evaluate(dev_rows, mode=mode, params=params)  # type: ignore[arg-type]
        test_metrics = evaluate(test_rows, mode=mode, params=params)  # type: ignore[arg-type]
        mode_reports[mode] = {
            "config": params,
            "dev": _round_metrics(dev_metrics),
            "test": _round_metrics(test_metrics),
        }
        recall = metric_value(dev_metrics["recall_at_5"])
        reciprocal = metric_value(dev_metrics["mrr"])
        if (recall, reciprocal) > best_recall:
            best_recall = (recall, reciprocal)
            selected_mode = mode
            selected_params = dict(params)
    if not tune_dev and existing is not None:
        selected_mode = str(existing["retrieval_mode"])
        selected_params = {
            "retrieval_mode": selected_mode,
            "rrf_k": existing["rrf_k"],
            "dense_pool": existing.get("dense_pool", 80),
            "lexical_pool": existing.get("lexical_pool", 80),
            "evidence_score_min": existing["evidence_score_min"],
            "rare_coverage_min": existing["rare_coverage_min"],
            "cite_margin": existing["cite_margin"],
            "cite_k": existing["cite_k"],
        }
    else:
        selected_params["retrieval_mode"] = selected_mode
        selected_params["dense_pool"] = 80
        selected_params["lexical_pool"] = 80
        calibration_payload = {
            "embedder_fingerprint": fingerprint,
            "dataset": "v1",
            "split_used": "dev",
            "tuned_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "git_sha": _git_sha(root),
            "retrieval_mode": selected_mode,
            "rrf_k": selected_params["rrf_k"],
            "evidence_score_min": selected_params["evidence_score_min"],
            "rare_coverage_min": selected_params["rare_coverage_min"],
            "cite_margin": selected_params["cite_margin"],
            "cite_k": selected_params["cite_k"],
            "dense_pool": 80,
            "lexical_pool": 80,
        }
        target_cal_path = repo_cal_path if commit else runtime_cal_path
        target_cal_path.parent.mkdir(parents=True, exist_ok=True)
        target_cal_path.write_text(
            json.dumps(calibration_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    git_sha = _git_sha(root)
    report = {
        "label": f"{slug}-v1",
        "dataset": "v1",
        "generated_at": generated_at,
        "git_sha": git_sha,
        "embedder_fingerprint": fingerprint,
        "embedder": settings.embedder,
        "embedding_model_when_bge": settings.embedding_model,
        "neural_weights_used": settings.embedder != "hashing",
        "protocol": (
            "Thresholds, citation margin, and RRF k are chosen on the dev split only. "
            "The test split is scored once with the frozen config. "
            "Refusal uses cosine similarity, or the lexical reranker score when that mode is active. "
            "Min-max normalized fusion scores are not used as a threshold. "
            "Hybrid fusion is reciprocal rank fusion, not a weighted sum."
        ),
        "selected_mode": selected_mode,
        "selected_config": selected_params,
        "selection_rule": "highest dev Recall@5, tie-break MRR; thresholds maximize dev refusal correctness then citation F1",
        "modes": mode_reports,
        "questions": {"total": len(questions), "dev": sum(1 for q in questions if q["split"] == "dev"), "test": sum(1 for q in questions if q["split"] == "test")},
    }
    if tune_dev:
        target_results_path = repo_results_path if commit else runtime_results_path
        target_results_path.parent.mkdir(parents=True, exist_ok=True)
        target_results_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({"selected_mode": selected_mode, "test": mode_reports[selected_mode]["test"]}, ensure_ascii=False, indent=2))  # type: ignore[index]
    if check:
        if not repo_results_path.is_file():
            print(f"该嵌入器没有已提交的基线: {repo_results_path}", file=sys.stderr)
            await dispose_engine()
            raise SystemExit(3)
        committed = json.loads(repo_results_path.read_text(encoding="utf-8"))
        current = mode_reports[selected_mode]["test"]  # type: ignore[index]
        reference = committed["modes"][selected_mode]["test"]
        for key in ("recall_at_5", "mrr", "refusal_correctness", "citation_f1"):
            delta = abs(metric_value(current[key]) - metric_value(reference[key]))  # type: ignore[index]
            if delta > 0.03:
                await dispose_engine()
                raise SystemExit(f"{key} drifted by {delta:.3f}")
    await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tune", action="store_true", help="Fit thresholds on dev and write calibration")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="With --tune, write calibration and baseline results to the repository",
    )
    parser.add_argument("--check", action="store_true", help="Fail if test metrics drift from the committed baseline")
    args = parser.parse_args()
    command.upgrade(Config(str(repo_root() / "backend" / "alembic.ini")), "head")
    asyncio.run(_run(tune_dev=args.tune, check=args.check, commit=args.commit))


if __name__ == "__main__":
    main()
