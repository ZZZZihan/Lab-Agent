"""Evaluate grounded retrieval. Thresholds are fit on the dev split only."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from app.auth import UNUSABLE_PASSWORD_HASH
from app.config import get_settings, repo_root
from app.db import dispose_engine, get_sessionmaker
from app.models import User
from app.rag.chunker import chunk_document
from app.rag.embedder import Embedder, build_embedder
from app.rag.ingest import ingest_text, read_corpus_dir, replace_source
from app.rag.retrieve import search_knowledge
from app.rag.tokenize import content_tokens
from sqlalchemy import select, text

from evals.metrics import _percentile, evaluate, metric_value, tune

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
    for meta, body in _read_corpus(corpus_dir):
        key = meta["key"]
        source_format = meta.get("source_format") or "markdown"
        seen: set[str] = set()
        for chunk in chunk_document(body, source_format):
            if chunk.heading in seen:
                raise RuntimeError(f"duplicate heading {key} / {chunk.heading}")
            seen.add(chunk.heading)
            mapping[(key, chunk.heading)] = f"{key}#{chunk.index}"
    return mapping


def _read_corpus(corpus_dir: Path) -> list[tuple[dict[str, str], str]]:
    return read_corpus_dir(corpus_dir)


def compute_dataset_manifest(dataset_dir: Path) -> dict[str, object]:
    files: dict[str, str] = {}
    for path in sorted(dataset_dir.rglob("*")):
        if not path.is_file() or path.name == "MANIFEST.json":
            continue
        rel = path.relative_to(dataset_dir).as_posix()
        files[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    questions_file = dataset_dir / "questions.jsonl"
    test_lines: list[str] = []
    if questions_file.is_file():
        for line in questions_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("split") == "test":
                    test_lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    test_split_sha256 = hashlib.sha256("\n".join(test_lines).encode("utf-8")).hexdigest()
    return {
        "dataset": dataset_dir.name,
        "test_split_sha256": test_split_sha256,
        "files": files,
    }


def verify_dataset_manifest(dataset_dir: Path) -> dict[str, object]:
    manifest_path = dataset_dir / "MANIFEST.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Missing dataset manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = compute_dataset_manifest(dataset_dir)
    recorded_files = manifest.get("files")
    if not isinstance(recorded_files, dict):
        raise RuntimeError(f"Invalid MANIFEST.json structure in {manifest_path}")
    actual_files = expected["files"]
    assert isinstance(actual_files, dict)
    if set(recorded_files.keys()) != set(actual_files.keys()):
        missing = sorted(set(recorded_files.keys()) - set(actual_files.keys()))
        extra = sorted(set(actual_files.keys()) - set(recorded_files.keys()))
        raise RuntimeError(
            f"Dataset manifest file list mismatch: missing={missing}, extra={extra}"
        )
    for rel_path, expected_sha in recorded_files.items():
        actual_sha = actual_files[rel_path]
        if actual_sha != expected_sha:
            raise RuntimeError(
                f"Dataset file hash mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}"
            )
    if (
        "test_split_sha256" in manifest
        and manifest["test_split_sha256"] != expected["test_split_sha256"]
    ):
        raise RuntimeError("Dataset test_split_sha256 mismatch in MANIFEST.json")
    return manifest


async def _prepare(settings: object, corpus_dir: Path | None = None) -> User:
    from app.config import Settings
    from app.ingest_worker import process_next_pending
    from app.rag.index_meta import column_type
    from app.reindex import apply_reindex

    assert isinstance(settings, Settings)
    resolved_corpus_dir = corpus_dir or Path(settings.corpus_dir)
    embedder = build_embedder(settings)
    maker = get_sessionmaker()
    try:
        rebuilt = False
        async with maker() as session:
            col_storage, col_dim = await column_type(session)
            if (col_storage, col_dim) != (settings.embedding_storage, settings.embedding_dim):
                rebuilt = await apply_reindex(session, settings, embedder, if_needed=True)
        if rebuilt:
            while await process_next_pending(embedder, settings) is not None:
                pass

        async with maker() as session:
            user = await session.scalar(select(User).where(User.email == "eval@lab.local"))
            if user is None:
                user = User(
                    email="eval@lab.local",
                    display_name="评测",
                    role="member",
                    password_hash=UNUSABLE_PASSWORD_HASH,
                    is_active=False,
                )
                session.add(user)
                await session.flush()
            else:
                user.role = "member"
                user.password_hash = UNUSABLE_PASSWORD_HASH
                user.is_active = False
                await session.flush()
            count = await replace_source(
                session,
                embedder=embedder,
                owner_id=user.id,
                source="eval",
                records=_read_corpus(resolved_corpus_dir),
            )
            seed_only_dir = resolved_corpus_dir.parent / "seed_only"
            if seed_only_dir.is_dir():
                seed_only_records = _read_corpus(seed_only_dir)
                if seed_only_records:
                    acl_owner = await session.scalar(
                        select(User).where(User.email == "eval-acl-owner@qingheng.lab")
                    )
                    if acl_owner is None:
                        acl_owner = User(
                            email="eval-acl-owner@qingheng.lab",
                            display_name="评测ACL所有者",
                            role="admin",
                            password_hash=UNUSABLE_PASSWORD_HASH,
                            is_active=False,
                        )
                        session.add(acl_owner)
                        await session.flush()
                    else:
                        acl_owner.role = "admin"
                        acl_owner.password_hash = UNUSABLE_PASSWORD_HASH
                        acl_owner.is_active = False
                        await session.flush()
                    for meta, body in seed_only_records:
                        key = meta.get("key")
                        await ingest_text(
                            session,
                            embedder=embedder,
                            owner_id=acl_owner.id,
                            title=meta.get("title") or key or "未命名",
                            text=body,
                            collection=meta.get("collection") or "knowledge",
                            visibility=meta.get("visibility") or "private",
                            source="eval",
                            source_format=meta.get("source_format") or "markdown",
                            doc_key=key,
                            storage=settings.embedding_storage,
                        )
            await session.commit()
            print(f"ingested {count} eval documents with embedder={embedder.name}")
            return user
    finally:
        await embedder.aclose()


class _TimingEmbedder:
    """Wraps an Embedder to record the latency (in ms) of the most recent embed_query call."""

    def __init__(self, inner: Embedder) -> None:
        self._inner = inner
        self.name: str = inner.name
        self.kind: str = inner.kind
        self.dim: int = inner.dim
        self.last_query_ms: float = 0.0

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        started = time.perf_counter()
        vec = await self._inner.embed_query(text)
        self.last_query_ms = (time.perf_counter() - started) * 1000.0
        return vec

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _chunks_table_bytes() -> int:
    maker = get_sessionmaker()
    async with maker() as session:
        size = await session.scalar(text("SELECT pg_total_relation_size('chunks')"))
        return int(size or 0)


def _latency_summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values) or [0.0]
    mean = sum(values) / len(values) if values else 0.0
    p50 = ordered[len(ordered) // 2]
    p95 = _percentile(ordered, 0.95)
    return {
        "mean": round(mean, 4),
        "p50": round(p50, 4),
        "p95": round(p95, 4),
    }


async def _featurize(
    settings: object,
    user: User,
    questions: list[dict[str, object]],
    corpus_dir: Path | None = None,
) -> list[dict[str, object]]:
    from app.acl import Principal, load_user_group_ids
    from app.config import Settings

    assert isinstance(settings, Settings)
    resolved_corpus_dir = corpus_dir or Path(settings.corpus_dir)
    embedder = _TimingEmbedder(build_embedder(settings))
    gold_map = _gold_map(resolved_corpus_dir)
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
                        "embed_ms": embedder.last_query_ms,
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


def _is_v1_default_path(raw: str | Path, suffix_parts: tuple[str, ...]) -> bool:
    p = Path(raw)
    return len(p.parts) >= len(suffix_parts) and p.parts[-len(suffix_parts) :] == suffix_parts


def _resolve_corpus_dir(settings: object, dataset: str = "v1") -> Path:
    from app.config import Settings

    assert isinstance(settings, Settings)
    root = repo_root()
    if dataset == "v1":
        return Path(settings.corpus_dir)
    dataset_corpus = root / "evals" / "data" / dataset / "corpus"
    env_override = os.environ.get("CORPUS_DIR", "").strip()
    if env_override and not _is_v1_default_path(env_override, ("evals", "data", "v1", "corpus")):
        return Path(env_override)
    if (
        settings.corpus_dir
        and not _is_v1_default_path(settings.corpus_dir, ("evals", "data", "v1", "corpus"))
    ):
        return Path(settings.corpus_dir)
    return dataset_corpus


def _resolve_questions_path(settings: object, dataset: str = "v1") -> Path:
    from app.config import Settings

    assert isinstance(settings, Settings)
    root = repo_root()
    if dataset == "v1":
        return Path(settings.questions_path)
    dataset_questions = root / "evals" / "data" / dataset / "questions.jsonl"
    env_override = os.environ.get("QUESTIONS_PATH", "").strip()
    if env_override and not _is_v1_default_path(
        env_override, ("evals", "data", "v1", "questions.jsonl")
    ):
        return Path(env_override)
    if (
        settings.questions_path
        and not _is_v1_default_path(
            settings.questions_path, ("evals", "data", "v1", "questions.jsonl")
        )
    ):
        return Path(settings.questions_path)
    return dataset_questions


async def _run(
    tune_dev: bool,
    check: bool,
    commit: bool = False,
    dataset: str = "v1",
) -> None:
    settings = get_settings()
    root = repo_root()
    slug = settings.embedding_slug
    fingerprint = settings.embedding_fingerprint
    runtime_dir = Path(settings.calibration_dir)
    corpus_dir = _resolve_corpus_dir(settings, dataset)
    questions_path = _resolve_questions_path(settings, dataset)
    if dataset == "v1":
        repo_cal_path = root / "evals" / "calibrations" / f"{slug}.json"
        repo_results_path = root / "evals" / "results" / "v1" / f"{slug}.json"
        runtime_cal_path = runtime_dir / f"{slug}.json"
        runtime_results_path = runtime_dir / f"results-{slug}.json"
    else:
        dataset_dir = root / "evals" / "data" / dataset
        verify_dataset_manifest(dataset_dir)
        repo_cal_path = root / "evals" / "calibrations" / dataset / f"{slug}.json"
        repo_results_path = root / "evals" / "results" / dataset / f"{slug}.json"
        runtime_cal_path = runtime_dir / dataset / f"{slug}.json"
        runtime_results_path = runtime_dir / dataset / f"results-{slug}.json"

    if check and not (tune_dev and commit) and not repo_results_path.is_file():
        print(f"该嵌入器没有已提交的基线: {repo_results_path}", file=sys.stderr)
        raise SystemExit(3)

    questions = _load_questions(questions_path)
    if len(questions) < 60:
        raise SystemExit(f"eval set has {len(questions)} questions, need at least 60")
    user = await _prepare(settings, corpus_dir=corpus_dir)
    features = await _featurize(settings, user, questions, corpus_dir=corpus_dir)
    chunks_table_bytes = await _chunks_table_bytes()
    embed_latencies = [float(row.get("embed_ms") or 0.0) for row in features]  # type: ignore[arg-type]
    search_latencies = [float(row.get("elapsed_ms") or 0.0) for row in features]  # type: ignore[arg-type]
    query_embedding_latency_ms = _latency_summary(embed_latencies)
    search_latency_ms = _latency_summary(search_latencies)

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
            "dataset": dataset,
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
    reindex_duration_raw = os.environ.get("EVAL_REINDEX_DURATION_S")
    reindex_duration_s: float | None = None
    if reindex_duration_raw:
        try:
            reindex_duration_s = round(float(reindex_duration_raw), 4)
        except ValueError:
            reindex_duration_s = None
    report: dict[str, object] = {
        "label": f"{slug}-{dataset}",
        "dataset": dataset,
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
        "chunks_table_bytes": chunks_table_bytes,
        "query_embedding_latency_ms": query_embedding_latency_ms,
        "search_latency_ms": search_latency_ms,
        "modes": mode_reports,
        "questions": {"total": len(questions), "dev": sum(1 for q in questions if q["split"] == "dev"), "test": sum(1 for q in questions if q["split"] == "test")},
    }
    if reindex_duration_s is not None:
        report["reindex_duration_s"] = reindex_duration_s
    if tune_dev:
        target_results_path = repo_results_path if commit else runtime_results_path
        target_results_path.parent.mkdir(parents=True, exist_ok=True)
        target_results_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "dataset": dataset,
                "selected_mode": selected_mode,
                "chunks_table_bytes": chunks_table_bytes,
                "query_embedding_latency_ms": query_embedding_latency_ms,
                "search_latency_ms": search_latency_ms,
                "test": mode_reports[selected_mode]["test"],  # type: ignore[index]
            },
            ensure_ascii=False,
            indent=2,
        )
    )
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
    parser.add_argument(
        "--dataset",
        choices=("v1", "v2"),
        default=os.environ.get("DATASET_VERSION", "v1"),
        help="Dataset version to evaluate (default: v1)",
    )
    parser.add_argument("--tune", action="store_true", help="Fit thresholds on dev and write calibration")
    parser.add_argument(
        "--commit",
        action="store_true",
        help="With --tune, write calibration and baseline results to the repository",
    )
    parser.add_argument("--check", action="store_true", help="Fail if test metrics drift from the committed baseline")
    args = parser.parse_args()
    command.upgrade(Config(str(repo_root() / "backend" / "alembic.ini")), "head")
    asyncio.run(_run(tune_dev=args.tune, check=args.check, commit=args.commit, dataset=args.dataset))


if __name__ == "__main__":
    main()
