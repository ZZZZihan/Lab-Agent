"""End-to-end generation evaluation across models via run_agent (Spec T3.4 / PR-24)."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import subprocess
import time
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from alembic import command
from alembic.config import Config
from app.agent import run_agent
from app.auth import UNUSABLE_PASSWORD_HASH
from app.config import Settings, get_settings, repo_root
from app.db import dispose_engine, get_sessionmaker
from app.llm import LLM, build_llm
from app.models import Document, User
from app.rag.chunker import chunk_markdown, parse_front_matter
from app.rag.embedder import build_embedder
from app.rag.index_meta import column_type
from app.rag.ingest import upsert_source
from app.reindex import apply_reindex
from app.secrets import read_secret
from sqlalchemy import select

from evals.metrics import _percentile

_CN_NUMBER_MAP: tuple[tuple[str, str], ...] = (
    ("一万二千", "12000"),
    ("五万", "50000"),
    ("二十四", "24"),
    ("六十", "60"),
    ("十一", "11"),
    ("十二", "12"),
    ("两个", "2个"),
    ("两块", "2块"),
    ("两周", "2周"),
    ("两张", "2张"),
    ("两台", "2台"),
    ("两小时", "2小时"),
    ("八台", "8台"),
    ("四张", "4张"),
    ("一次", "1次"),
    ("二次", "2次"),
    ("两次", "2次"),
)

_THOUSANDS_COMMA_RE = re.compile(r"(?<=\d),(?=\d{3}(?:\D|$))")
_STRIP_PUNCT_WS_RE = re.compile(
    r"[\s　,，。、；;：:！!？?\"'“”‘’`（）()【】\[\]《》<>·—–\-]+"
)
_CITATION_TAG_RE = re.compile(r"\[([0-9]{1,2})\]")


def normalize_for_fact_match(text: str) -> str:
    """Normalize text for key-fact substring matching (NFKC, numbers, whitespace/punct)."""
    folded = unicodedata.normalize("NFKC", text).lower()
    folded = _THOUSANDS_COMMA_RE.sub("", folded)
    for src, dst in _CN_NUMBER_MAP:
        folded = folded.replace(src, dst)
    folded = _STRIP_PUNCT_WS_RE.sub("", folded)
    return folded


def compute_fact_recall(
    answer: str,
    answer_facts: Sequence[str],
    *,
    refused: bool = False,
) -> float:
    if not answer_facts:
        return 0.0 if refused else 1.0
    if refused or not answer.strip():
        return 0.0
    norm_answer = normalize_for_fact_match(answer)
    if not norm_answer:
        return 0.0
    matched = 0
    for fact in answer_facts:
        norm_fact = normalize_for_fact_match(str(fact))
        if norm_fact and norm_fact in norm_answer:
            matched += 1
    return matched / len(answer_facts)


def slugify_model(model_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model_name.strip().lower()).strip("-")


def detect_model_vendor(model_name: str) -> str:
    raw = model_name.strip().lower()
    if not raw:
        return "unknown"
    tail = raw.rsplit("/", 1)[-1]
    for candidate in (tail, raw):
        if any(k in candidate for k in ("claude", "anthropic", "fable", "opus", "sonnet", "haiku")):
            return "anthropic"
        if any(k in candidate for k in ("gemini", "google", "gemma")):
            return "google"
        if any(k in candidate for k in ("gpt", "openai")) or re.search(
            r"(?:^|[/\-_])o[134](?:[/\-_]|$)", candidate
        ):
            return "openai"
        if any(k in candidate for k in ("grok", "xai")):
            return "xai"
        if "deepseek" in candidate:
            return "deepseek"
        if "qwen" in candidate:
            return "qwen"
        if any(k in candidate for k in ("llama", "meta")):
            return "meta"
        if any(k in candidate for k in ("mistral", "mixtral")):
            return "mistral"
        if any(k in candidate for k in ("glm", "zhipu")):
            return "zhipu"
        if any(k in candidate for k in ("kimi", "moonshot")):
            return "moonshot"
        if candidate.startswith("fake"):
            return "fake"
    if "/" in raw:
        return raw.split("/", 1)[0]
    return tail.split("-", 1)[0]


def validate_judge_model(llm_model: str, judge_model: str) -> None:
    clean_llm = llm_model.strip()
    clean_judge = judge_model.strip()
    if not clean_judge:
        raise ValueError("JUDGE_MODEL must be non-empty when --judge is enabled")
    if clean_llm.lower() == clean_judge.lower():
        raise ValueError(
            f"Judge model {clean_judge!r} must not be identical to evaluated model {clean_llm!r}"
        )
    llm_vendor = detect_model_vendor(clean_llm)
    judge_vendor = detect_model_vendor(clean_judge)
    if llm_vendor == judge_vendor:
        raise ValueError(
            f"Judge model {clean_judge!r} (vendor={judge_vendor}) must come from a different "
            f"vendor family than evaluated model {clean_llm!r} (vendor={llm_vendor})"
        )


def _load_questions(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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
    records: list[tuple[dict[str, str], str]] = []
    for file in sorted(corpus_dir.glob("*.md")):
        records.append(parse_front_matter(file.read_text(encoding="utf-8")))
    return records


def _latency_summary(samples: Sequence[float]) -> dict[str, float]:
    if not samples:
        return {"mean": 0.0, "p50": 0.0, "p95": 0.0}
    vals = sorted(float(x) for x in samples)
    return {
        "mean": round(sum(vals) / len(vals), 2),
        "p50": round(_percentile(vals, 0.50), 2),
        "p95": round(_percentile(vals, 0.95), 2),
    }


def score_question_e2e(
    question_row: Mapping[str, Any],
    *,
    answer: str,
    refused: bool,
    decision_reason: str,
    initial_citations: Sequence[Mapping[str, Any]],
    citation_check_event: Mapping[str, Any] | None,
    gold_keys: Sequence[str],
    ttft_ms: float | None,
    total_latency_ms: float,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    answerable = bool(question_row["answerable"])
    e2e_refused = bool(
        refused
        or decision_reason
        in {
            "low_score",
            "low_coverage",
            "no_hits",
            "insufficient_evidence",
            "model_declined",
        }
    )

    refusal_correct = 1.0 if (answerable != e2e_refused) else 0.0
    false_refusal = (1.0 if e2e_refused else 0.0) if answerable else None
    false_answer = (1.0 if not e2e_refused else 0.0) if not answerable else None

    raw_used = (citation_check_event or {}).get("used")
    used_indices: list[int] = (
        [int(i) for i in raw_used if isinstance(i, int)]
        if isinstance(raw_used, list)
        else []
    )
    raw_invalid = (citation_check_event or {}).get("invalid")
    invalid_indices: list[int] = (
        [int(i) for i in raw_invalid if isinstance(i, int)]
        if isinstance(raw_invalid, list)
        else []
    )
    uncited = bool((citation_check_event or {}).get("uncited"))

    cite_by_index: dict[int, str] = {}
    cite_text_by_index: dict[int, str] = {}
    for item in initial_citations:
        idx = item.get("index")
        doc_key = item.get("doc_key")
        chunk_index = item.get("chunk_index")
        if isinstance(idx, int) and doc_key is not None and chunk_index is not None:
            cite_by_index[idx] = f"{doc_key}#{chunk_index}"
            title = str(item.get("title") or "")
            heading = str(item.get("heading") or "")
            excerpt = str(item.get("excerpt") or "")
            cite_text_by_index[idx] = f"[{idx}] 《{title}》·{heading}: {excerpt}".strip()

    used_keys: list[str] = []
    for idx in used_indices:
        mapped = cite_by_index.get(idx)
        if mapped and mapped not in used_keys:
            used_keys.append(mapped)

    if answerable:
        if e2e_refused or not used_keys or not gold_keys:
            c_prec = 0.0
            c_rec = 0.0
            c_f1 = 0.0
        else:
            gold_set = set(gold_keys)
            hits = sum(1 for k in used_keys if k in gold_set)
            c_prec = hits / len(used_keys)
            c_rec = hits / len(gold_set)
            c_f1 = (2 * c_prec * c_rec / (c_prec + c_rec)) if (c_prec + c_rec) > 0 else 0.0
    else:
        c_prec = None
        c_rec = None
        c_f1 = None

    has_invalid = (1.0 if len(invalid_indices) > 0 else 0.0) if not e2e_refused else None
    is_unannotated = (1.0 if uncited else 0.0) if not e2e_refused else None

    raw_facts = question_row.get("answer_facts")
    facts_list = [str(f) for f in raw_facts] if isinstance(raw_facts, list) else []
    fact_recall = (
        compute_fact_recall(answer, facts_list, refused=e2e_refused)
        if answerable
        else None
    )

    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    returned_model = str(usage.get("model") or "")

    return {
        "id": str(question_row["id"]),
        "split": str(question_row["split"]),
        "type": str(question_row["type"]),
        "question": str(question_row["question"]),
        "answerable": answerable,
        "gold": list(gold_keys),
        "answer_facts": facts_list,
        "answer": answer,
        "refused": e2e_refused,
        "decision_reason": decision_reason,
        "used_indices": used_indices,
        "invalid_indices": invalid_indices,
        "uncited": uncited,
        "used_citation_keys": used_keys,
        "cite_text_by_index": cite_text_by_index,
        "refusal_correct": refusal_correct,
        "false_refusal": false_refusal,
        "false_answer": false_answer,
        "citation_precision": c_prec,
        "citation_recall": c_rec,
        "citation_f1": c_f1,
        "has_invalid_citation": has_invalid,
        "is_unannotated": is_unannotated,
        "fact_recall": fact_recall,
        "ttft_ms": round(ttft_ms, 2) if ttft_ms is not None else None,
        "total_latency_ms": round(total_latency_ms, 2),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "returned_model": returned_model,
    }


def _mean_or_zero(vals: Sequence[float]) -> float:
    if not vals:
        return 0.0
    return round(sum(vals) / len(vals), 4)


def aggregate_e2e_metrics(question_results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    refusal_vals = [float(r["refusal_correct"]) for r in question_results]
    false_ref_vals = [
        float(r["false_refusal"])
        for r in question_results
        if r.get("false_refusal") is not None
    ]
    false_ans_vals = [
        float(r["false_answer"])
        for r in question_results
        if r.get("false_answer") is not None
    ]
    c_prec_vals = [
        float(r["citation_precision"])
        for r in question_results
        if r.get("citation_precision") is not None
    ]
    c_rec_vals = [
        float(r["citation_recall"])
        for r in question_results
        if r.get("citation_recall") is not None
    ]
    c_f1_vals = [
        float(r["citation_f1"])
        for r in question_results
        if r.get("citation_f1") is not None
    ]
    invalid_vals = [
        float(r["has_invalid_citation"])
        for r in question_results
        if r.get("has_invalid_citation") is not None
    ]
    unannot_vals = [
        float(r["is_unannotated"])
        for r in question_results
        if r.get("is_unannotated") is not None
    ]
    fact_vals = [
        float(r["fact_recall"])
        for r in question_results
        if r.get("fact_recall") is not None
    ]
    ttft_vals = [
        float(r["ttft_ms"])
        for r in question_results
        if r.get("ttft_ms") is not None
    ]
    total_lat_vals = [float(r["total_latency_ms"]) for r in question_results]

    prompt_total = sum(int(r.get("prompt_tokens") or 0) for r in question_results)
    completion_total = sum(int(r.get("completion_tokens") or 0) for r in question_results)
    tokens_total = prompt_total + completion_total
    n_q = max(len(question_results), 1)

    returned_models: list[str] = []
    for r in question_results:
        rm = str(r.get("returned_model") or "").strip()
        if rm and rm not in returned_models:
            returned_models.append(rm)

    return {
        "e2e_refusal_correctness": _mean_or_zero(refusal_vals),
        "false_refusal_rate": _mean_or_zero(false_ref_vals),
        "false_answer_rate": _mean_or_zero(false_ans_vals),
        "citation_precision": _mean_or_zero(c_prec_vals),
        "citation_recall": _mean_or_zero(c_rec_vals),
        "citation_f1": _mean_or_zero(c_f1_vals),
        "invalid_citation_rate": _mean_or_zero(invalid_vals),
        "unannotated_rate": _mean_or_zero(unannot_vals),
        "answer_fact_recall": _mean_or_zero(fact_vals),
        "ttft_ms": _latency_summary(ttft_vals),
        "total_latency_ms": _latency_summary(total_lat_vals),
        "usage": {
            "prompt_tokens_total": prompt_total,
            "completion_tokens_total": completion_total,
            "total_tokens": tokens_total,
            "prompt_tokens_mean": round(prompt_total / n_q, 2),
            "completion_tokens_mean": round(completion_total / n_q, 2),
            "total_tokens_mean": round(tokens_total / n_q, 2),
            "returned_models": returned_models,
        },
    }


def _extract_judgable_sentences(answer: str) -> list[str]:
    sentences: list[str] = []
    for raw_line in re.split(r"(?<=[。！？\n])", answer):
        cleaned = raw_line.strip()
        if not cleaned:
            continue
        if cleaned in {
            "根据实验室文档中可核对的段落，回答如下。",
            "以上内容只复述检索到的原文，没有使用这些段落之外的信息。",
        }:
            continue
        sentences.append(cleaned)
    return sentences


def _resolve_judge_api_key(settings: Settings) -> str | None:
    raw_direct = (
        settings.judge_api_key.get_secret_value().strip()
        if settings.judge_api_key is not None
        else ""
    )
    if raw_direct:
        return raw_direct
    if settings.judge_api_key_file:
        expanded = Path(settings.judge_api_key_file).expanduser()
        if expanded.is_file():
            resolved = read_secret(
                None,
                settings.judge_api_key_file,
                "JUDGE_API_KEY",
                app_env=settings.app_env,
            )
            if resolved is not None:
                return resolved.get_secret_value()
    if settings.llm_api_key is not None and settings.llm_api_key.get_secret_value().strip():
        return settings.llm_api_key.get_secret_value().strip()
    return None


async def run_judge_on_results(
    *,
    settings: Settings,
    llm_model: str,
    question_results: Sequence[Mapping[str, Any]],
    audit_csv_path: Path,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    judge_model = settings.judge_model.strip()
    validate_judge_model(llm_model, judge_model)
    base_url = (settings.judge_base_url or settings.llm_base_url or "").rstrip("/")
    if not base_url:
        raise ValueError("JUDGE_BASE_URL or LLM_BASE_URL must be configured when --judge is enabled")

    api_key = _resolve_judge_api_key(settings)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    judged_rows: list[dict[str, Any]] = []
    timeout = httpx.Timeout(timeout=float(settings.llm_timeout_seconds), connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        for q_res in question_results:
            if q_res.get("refused"):
                continue
            answer = str(q_res.get("answer") or "")
            cite_map = q_res.get("cite_text_by_index") or {}
            if not isinstance(cite_map, dict):
                cite_map = {}
            for sentence in _extract_judgable_sentences(answer):
                cited_nums = [int(m.group(1)) for m in _CITATION_TAG_RE.finditer(sentence)]
                if cited_nums:
                    ev_texts = [
                        str(cite_map[idx]) for idx in cited_nums if idx in cite_map
                    ]
                else:
                    ev_texts = [str(v) for v in cite_map.values()]
                cited_evidence = " | ".join(ev_texts)
                if not cited_evidence:
                    score = 0
                else:
                    prompt = (
                        "请判断下述【待核对句子】中的事实陈述是否完全被【所引证据】支持。\n"
                        "如果完全支持，只输出数字 1；如果存在证据未提及或与证据矛盾的内容，只输出数字 0。\n\n"
                        f"【所引证据】\n{cited_evidence}\n\n"
                        f"【待核对句子】\n{sentence}"
                    )
                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        headers=headers,
                        json={
                            "model": judge_model,
                            "messages": [
                                {
                                    "role": "system",
                                    "content": "你是严格的事实核查裁判。只输出 1 或 0。",
                                },
                                {"role": "user", "content": prompt},
                            ],
                            "temperature": 0.0,
                            "max_tokens": 8,
                            "stream": False,
                        },
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    content = ""
                    choices = body.get("choices") if isinstance(body, dict) else None
                    if isinstance(choices, list) and choices:
                        first = choices[0]
                        if isinstance(first, dict):
                            msg = first.get("message") or first.get("delta") or {}
                            if isinstance(msg, dict):
                                content = str(msg.get("content") or "").strip()
                    score = 1 if content.startswith("1") or ("1" in content and "0" not in content) else 0

                judged_rows.append(
                    {
                        "question_id": str(q_res["id"]),
                        "sentence": sentence,
                        "cited_evidence": cited_evidence,
                        "judge_score": score,
                        "human_score": "",
                        "notes": "",
                    }
                )

    rng = random.Random(20261105)
    sample_size = min(30, len(judged_rows))
    sampled = rng.sample(judged_rows, k=sample_size) if sample_size > 0 else []

    audit_csv_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "question_id",
                "sentence",
                "cited_evidence",
                "judge_score",
                "human_score",
                "notes",
            ],
        )
        writer.writeheader()
        for row in sampled:
            writer.writerow(row)

    faithfulness = (
        round(sum(int(r["judge_score"]) for r in judged_rows) / len(judged_rows), 4)
        if judged_rows
        else 0.0
    )
    return {
        "judge_model": judge_model,
        "judge_vendor": detect_model_vendor(judge_model),
        "faithfulness_score": faithfulness,
        "n_sentences": len(judged_rows),
        "sampled_audit_rows": len(sampled),
        "human_audit_csv": str(audit_csv_path),
        "human_reviewed": False,
        "human_agreement": None,
    }


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


async def _run_single_model_questions(
    *,
    session: Any,
    user: User,
    settings: Settings,
    embedder: Any,
    llm: LLM,
    questions: Sequence[Mapping[str, Any]],
    gold_map: Mapping[tuple[str, str], str],
) -> list[dict[str, Any]]:
    question_results: list[dict[str, Any]] = []
    for q in questions:
        gold_keys: list[str] = []
        raw_gold = q.get("gold") or []
        if isinstance(raw_gold, list):
            for ref in raw_gold:
                if isinstance(ref, dict):
                    gold_keys.append(gold_map[(str(ref["doc"]), str(ref["heading"]))])

        emitted: list[dict[str, object]] = []
        t0 = time.perf_counter()
        ttft_ms: float | None = None

        async def _emit(
            event: dict[str, object],
            _sink: list[dict[str, object]] = emitted,
            _start: float = t0,
        ) -> None:
            nonlocal ttft_ms
            if event.get("type") == "token" and ttft_ms is None:
                ttft_ms = (time.perf_counter() - _start) * 1000.0
            _sink.append(event)

        agent_out = await run_agent(
            session=session,
            user=user,
            settings=settings,
            embedder=embedder,
            llm=llm,
            question=str(q["question"]),
            emit=_emit,
            request_id=f"e2e-{q['id']}",
            group_ids=frozenset(),
        )
        total_ms = (time.perf_counter() - t0) * 1000.0
        emitted.append(
            {
                "type": "done",
                "refused": bool(agent_out.get("refused")),
                "usage": agent_out.get("usage"),
            }
        )
        await session.commit()

        initial_citations: list[Mapping[str, Any]] = []
        citation_check_event: Mapping[str, Any] | None = None
        for ev in emitted:
            if ev.get("type") == "citations":
                items = ev.get("items")
                if isinstance(items, list):
                    initial_citations = [c for c in items if isinstance(c, dict)]
            elif ev.get("type") == "citation_check":
                citation_check_event = ev

        if not initial_citations:
            raw_cites = agent_out.get("citations")
            if isinstance(raw_cites, list):
                initial_citations = [c for c in raw_cites if isinstance(c, dict)]

        decision = agent_out.get("decision")
        decision_reason = (
            str(decision.get("reason") or "") if isinstance(decision, dict) else ""
        )
        usage_dict = agent_out.get("usage")
        if not isinstance(usage_dict, dict):
            usage_dict = {}

        q_score = score_question_e2e(
            q,
            answer=str(agent_out.get("answer") or ""),
            refused=bool(agent_out.get("refused")),
            decision_reason=decision_reason,
            initial_citations=initial_citations,
            citation_check_event=citation_check_event,
            gold_keys=gold_keys,
            ttft_ms=ttft_ms,
            total_latency_ms=total_ms,
            usage=usage_dict,
        )
        question_results.append(q_score)
    return question_results


async def _run(
    *,
    dataset: str = "v1",
    split: str = "test",
    max_questions: int | None = None,
    models: Sequence[str] | None = None,
    use_judge: bool = False,
    commit: bool = False,
    check: bool = False,
    judge_transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    base_settings = get_settings()
    root = repo_root()
    corpus_dir = root / "evals" / "data" / dataset / "corpus"
    questions_path = root / "evals" / "data" / dataset / "questions.jsonl"
    if not corpus_dir.is_dir() or not questions_path.is_file():
        raise SystemExit(f"Unknown dataset {dataset!r}: missing {corpus_dir} or {questions_path}")

    all_questions = _load_questions(questions_path)
    selected_questions = (
        [q for q in all_questions if q.get("split") == split]
        if split != "all"
        else list(all_questions)
    )
    if max_questions is not None and max_questions > 0:
        selected_questions = selected_questions[:max_questions]
    if not selected_questions:
        raise SystemExit(f"No questions found for dataset={dataset!r} split={split!r}")

    model_list = (
        [m.strip() for m in models if m.strip()]
        if models
        else [base_settings.llm_model]
    )

    if use_judge:
        for m in model_list:
            validate_judge_model(m, base_settings.judge_model)

    gold_map = _gold_map(corpus_dir)
    corpus_records = _read_corpus(corpus_dir)
    embedder = build_embedder(base_settings)
    maker = get_sessionmaker()

    reports: list[dict[str, Any]] = []
    try:
        async with maker() as session:
            col_storage, col_dim = await column_type(session)
            if (col_storage, col_dim) != (
                base_settings.embedding_storage,
                base_settings.embedding_dim,
            ):
                await apply_reindex(session, base_settings, embedder, if_needed=True)

            user = await session.scalar(
                select(User).where(User.email == "eval@lab.local")
            )
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
                user.role = "admin"
                user.password_hash = UNUSABLE_PASSWORD_HASH
                user.is_active = False
                await session.flush()

            # Temporarily soft-hide non-corpus seed documents (such as private-review) during E2E
            corpus_keys = {meta["key"] for meta, _ in corpus_records if meta.get("key")}
            await upsert_source(
                session,
                embedder=embedder,
                owner_id=user.id,
                source="seed",
                records=corpus_records,
                storage=base_settings.embedding_storage,
            )
            other_docs = list(
                await session.scalars(
                    select(Document).where(
                        Document.deleted_at.is_(None),
                        Document.source != "eval",
                        Document.source != "seed",
                    )
                )
            )
            hidden_doc_ids = [doc.id for doc in other_docs if doc.doc_key not in corpus_keys]
            now_ts = datetime.now(UTC)
            for doc in other_docs:
                if doc.id in set(hidden_doc_ids):
                    doc.deleted_at = now_ts
            await session.commit()

            try:
                for model_name in model_list:
                    provider = base_settings.llm_provider
                    if (
                        provider == "fake"
                        and model_name != "fake-grounded-v1"
                        and base_settings.llm_base_url
                    ):
                        provider = "openai_compatible"
                    model_settings = base_settings.model_copy(
                        update={
                            "llm_provider": provider,
                            "llm_model": model_name,
                            "corpus_dir": str(corpus_dir),
                            "questions_path": str(questions_path),
                        }
                    )
                    llm = build_llm(model_settings)
                    try:
                        q_results = await _run_single_model_questions(
                            session=session,
                            user=user,
                            settings=model_settings,
                            embedder=embedder,
                            llm=llm,
                            questions=selected_questions,
                            gold_map=gold_map,
                        )
                    finally:
                        await llm.aclose()

                    metrics = aggregate_e2e_metrics(q_results)
                    llm_slug = slugify_model(model_name)
                    embed_slug = model_settings.embedding_slug
                    run_slug = f"{llm_slug}__{embed_slug}"

                    repo_e2e_dir = root / "evals" / "results" / "e2e"
                    runtime_e2e_dir = Path(model_settings.calibration_dir) / "e2e"
                    out_dir = repo_e2e_dir if commit else runtime_e2e_dir
                    out_path = out_dir / f"{run_slug}.json"

                    judge_section: dict[str, Any] | None = None
                    if use_judge:
                        audit_csv_path = out_dir / run_slug / "human_audit.csv"
                        judge_section = await run_judge_on_results(
                            settings=model_settings,
                            llm_model=model_name,
                            question_results=q_results,
                            audit_csv_path=audit_csv_path,
                            transport=judge_transport,
                        )
                        if commit:
                            judge_section["human_audit_csv"] = str(
                                audit_csv_path.relative_to(root)
                            )

                    generated_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    git_sha = _git_sha(root)
                    returned_ids = list(metrics["usage"]["returned_models"]) or [model_name]

                    report: dict[str, Any] = {
                        "label": f"{run_slug}-{dataset}-{split}",
                        "suite": "e2e",
                        "dataset": dataset,
                        "split": split,
                        "n_questions": len(selected_questions),
                        "n_answerable": sum(
                            1 for q in selected_questions if q.get("answerable")
                        ),
                        "n_unanswerable": sum(
                            1 for q in selected_questions if not q.get("answerable")
                        ),
                        "llm_provider": model_settings.llm_provider,
                        "llm_model": model_name,
                        "returned_model_ids": returned_ids,
                        "embedding_fingerprint": model_settings.embedding_fingerprint,
                        "embedding_slug": embed_slug,
                        "retrieval_mode": model_settings.retrieval_mode,
                        "date": generated_at[:10],
                        "generated_at": generated_at,
                        "git_sha": git_sha,
                        "metrics": metrics,
                        "judge": judge_section,
                        "questions": [
                            {
                                k: v
                                for k, v in item.items()
                                if k != "cite_text_by_index"
                            }
                            for item in q_results
                        ],
                    }

                    out_dir.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(
                        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    print(
                        json.dumps(
                            {
                                "llm_model": model_name,
                                "returned_model_ids": returned_ids,
                                "embedding_fingerprint": model_settings.embedding_fingerprint,
                                "n_questions": len(selected_questions),
                                "metrics": metrics,
                                "judge": judge_section,
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                    )

                    if check:
                        committed_path = repo_e2e_dir / f"{run_slug}.json"
                        if not committed_path.is_file():
                            await dispose_engine()
                            raise SystemExit(
                                f"Missing committed E2E baseline report: {committed_path}"
                            )
                    reports.append(report)
            finally:
                for doc in other_docs:
                    if doc.id in set(hidden_doc_ids):
                        doc.deleted_at = None
                await session.commit()
    finally:
        await embedder.aclose()
        await dispose_engine()

    return reports


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run end-to-end generation evaluation.")
    parser.add_argument("--dataset", default="v1", help="Dataset version (default: v1)")
    parser.add_argument("--split", default="test", help="Split to evaluate (default: test)")
    parser.add_argument(
        "--max-questions",
        type=int,
        default=None,
        help="Optional maximum number of questions to evaluate",
    )
    parser.add_argument(
        "--models",
        default="",
        help="Comma-separated list of LLM models to evaluate",
    )
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Run cross-vendor LLM-as-judge faithfulness evaluation and export human_audit.csv",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write E2E evaluation results under evals/results/e2e/",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify committed E2E result file exists",
    )
    args = parser.parse_args(argv)
    model_list = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else None
    command.upgrade(Config(str(repo_root() / "backend" / "alembic.ini")), "head")
    asyncio.run(
        _run(
            dataset=args.dataset,
            split=args.split,
            max_questions=args.max_questions,
            models=model_list,
            use_judge=args.judge,
            commit=args.commit,
            check=args.check,
        )
    )


if __name__ == "__main__":
    main()
