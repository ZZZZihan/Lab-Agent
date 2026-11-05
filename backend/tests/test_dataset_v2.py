"""Dataset v2 integrity, manifest freeze, PDF extraction, and report checks (Spec T3.5 / PR-25)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from app.config import repo_root
from app.rag.chunker import chunk_document
from app.rag.ingest import extract_pdf_text, read_corpus_dir
from app.rag.tokenize import content_tokens
from evals.report import run_report
from evals.run_eval import (
    _gold_map,
    _load_questions,
    _prepare,
    _resolve_corpus_dir,
    _resolve_questions_path,
    compute_dataset_manifest,
    verify_dataset_manifest,
)

ROOT = repo_root()
V2_DIR = ROOT / "evals" / "data" / "v2"
V2_CORPUS_DIR = V2_DIR / "corpus"
V2_SEED_ONLY_DIR = V2_DIR / "seed_only"
V2_QUESTIONS_PATH = V2_DIR / "questions.jsonl"

REQUIRED_TYPES = {
    "direct",
    "paraphrase",
    "synonym",
    "distractor",
    "multi_chunk",
    "numeric",
    "unanswerable_near",
    "unanswerable_far",
    "acl",
}


def test_v1_dataset_remains_untouched() -> None:
    v1_corpus = ROOT / "evals" / "data" / "v1" / "corpus"
    v1_questions = ROOT / "evals" / "data" / "v1" / "questions.jsonl"
    assert len(list(v1_corpus.glob("*.md"))) == 13
    assert len(_load_questions(v1_questions)) == 78


def test_v2_manifest_matches_all_files_and_detects_tampering(tmp_path: Path) -> None:
    manifest = verify_dataset_manifest(V2_DIR)
    files = manifest["files"]
    assert isinstance(files, dict)
    assert "questions.jsonl" in files
    assert sum(1 for rel in files if rel.startswith("corpus/")) >= 42
    assert sum(1 for rel in files if rel.startswith("seed_only/")) >= 2

    # Verify tamper detection on a temporary copy
    sample_dir = tmp_path / "v2"
    (sample_dir / "corpus").mkdir(parents=True)
    (sample_dir / "corpus" / "doc.md").write_text("---\nkey: doc\n---\n正文\n", encoding="utf-8")
    (sample_dir / "questions.jsonl").write_text(
        json.dumps({"id": "q1", "split": "test", "question": "测试"}) + "\n",
        encoding="utf-8",
    )
    sample_manifest = compute_dataset_manifest(sample_dir)
    (sample_dir / "MANIFEST.json").write_text(
        json.dumps(sample_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    verify_dataset_manifest(sample_dir)

    (sample_dir / "corpus" / "doc.md").write_text("---\nkey: doc\n---\n篡改\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        verify_dataset_manifest(sample_dir)


def test_v2_corpus_composition_pdf_extraction_and_unique_headings() -> None:
    records = read_corpus_dir(V2_CORPUS_DIR)
    assert len(records) >= 42

    collections_seen = {meta["collection"] for meta, _ in records}
    assert {"knowledge", "project", "meeting", "paper_note"} <= collections_seen

    no_h2_docs = [
        (meta, body)
        for meta, body in records
        if meta["source_format"] in {"markdown", "text"} and "## " not in body
    ]
    assert len(no_h2_docs) >= 5

    txt_docs = [(meta, body) for meta, body in records if meta["source_format"] == "text"]
    assert len(txt_docs) >= 5
    for _meta, body in txt_docs:
        chunks = chunk_document(body, "text")
        assert len(chunks) >= 2
        assert chunks[0].heading == "第 1 段"
        assert chunks[1].heading == "第 2 段"

    md_no_h2 = [
        (meta, body)
        for meta, body in records
        if meta["source_format"] == "markdown" and "## " not in body
    ]
    assert len(md_no_h2) >= 2
    for _meta, body in md_no_h2:
        chunks = chunk_document(body, "markdown")
        assert [c.heading for c in chunks] == ["概述"]

    pdf_files = sorted(V2_CORPUS_DIR.glob("*.pdf"))
    assert len(pdf_files) >= 3
    for pdf_file in pdf_files:
        extracted = extract_pdf_text(pdf_file)
        assert "\f" in extracted
        pdf_chunks = chunk_document(extracted, "pdf")
        assert [c.heading for c in pdf_chunks] == ["第 1 页", "第 2 页", "第 3 页"]
        for ch in pdf_chunks:
            assert len(ch.text) >= 40

    # Verify build_v2_pdfs.py --check confirms deterministic PDF bytes
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_v2_pdfs.py"), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    # Verify _gold_map succeeds and all (doc_key, heading) pairs are unique
    gold_map = _gold_map(V2_CORPUS_DIR)
    assert len(gold_map) >= 140


def test_v2_questions_schema_ratios_gold_facts_and_distractors() -> None:
    questions = _load_questions(V2_QUESTIONS_PATH)
    assert len(questions) >= 300

    dev_q = [q for q in questions if q.get("split") == "dev"]
    test_q = [q for q in questions if q.get("split") == "test"]
    assert len(dev_q) >= 140
    assert len(test_q) >= 140
    assert len(dev_q) + len(test_q) == len(questions)
    assert abs(len(dev_q) - len(test_q)) <= 20

    # Check unanswerable ratio >= 25% and unanswerable_near > 50% of unanswerable
    unanswerable = [q for q in questions if not q.get("answerable")]
    answerable = [q for q in questions if q.get("answerable")]
    assert len(unanswerable) / len(questions) >= 0.25
    assert len(unanswerable) >= 76

    near_unans = [q for q in unanswerable if q.get("type") == "unanswerable_near"]
    far_unans = [q for q in unanswerable if q.get("type") == "unanswerable_far"]
    acl_unans = [q for q in unanswerable if q.get("type") == "acl"]
    assert len(near_unans) > len(unanswerable) / 2
    assert len(near_unans) >= 40
    assert len(far_unans) >= 20
    assert len(acl_unans) >= 10

    # All 9 question types present overall and in both splits
    all_types = {str(q.get("type")) for q in questions}
    dev_types = {str(q.get("type")) for q in dev_q}
    test_types = {str(q.get("type")) for q in test_q}
    assert all_types == REQUIRED_TYPES
    assert dev_types == REQUIRED_TYPES
    assert test_types == REQUIRED_TYPES

    # Provenance fields on every question
    authored_values = set()
    ids_seen: set[str] = set()
    for q in questions:
        qid = str(q["id"])
        assert qid not in ids_seen, f"Duplicate question id: {qid}"
        ids_seen.add(qid)
        assert q.get("authored_by") in {"human", "llm_assisted"}
        authored_values.add(str(q["authored_by"]))
        assert q.get("reviewed") is True
    assert authored_values == {"human", "llm_assisted"}

    # Build chunk text lookup and full corpus text
    records = read_corpus_dir(V2_CORPUS_DIR)
    gold_map = _gold_map(V2_CORPUS_DIR)
    chunk_text_by_ref: dict[tuple[str, str], str] = {}
    all_corpus_text = "\n".join(body for _, body in records)
    all_corpus_tokens: set[str] = set()
    for meta, body in records:
        all_corpus_tokens.update(content_tokens(body))
        for ch in chunk_document(body, meta["source_format"]):
            chunk_text_by_ref[(meta["key"], ch.heading)] = ch.text

    # Answerable questions must have resolving gold and grounded answer_facts
    for q in answerable:
        gold = q.get("gold")
        assert isinstance(gold, list) and len(gold) >= 1
        if q.get("type") == "multi_chunk":
            assert len(gold) >= 2
        combined_gold_text = ""
        for ref in gold:
            assert isinstance(ref, dict)
            pair = (str(ref["doc"]), str(ref["heading"]))
            assert pair in gold_map, f"Unresolved gold {pair} in {q['id']}"
            combined_gold_text += "\n" + chunk_text_by_ref[pair]
        facts = q.get("answer_facts")
        assert isinstance(facts, list) and len(facts) >= 1
        for fact in facts:
            assert isinstance(fact, str) and fact.strip()
            assert fact in combined_gold_text, f"Fact {fact!r} not in gold chunk for {q['id']}"

    # Unanswerable questions must have empty gold/answer_facts and absent distractor_terms
    for q in unanswerable:
        assert q.get("gold") == []
        assert q.get("answer_facts") == []
        dterms = q.get("distractor_terms")
        assert isinstance(dterms, list) and len(dterms) >= 1
        q_text = str(q["question"])
        for term in dterms:
            assert isinstance(term, str) and term in q_text
            assert term not in all_corpus_text, (
                f"Distractor term {term!r} for {q['id']} ({q['type']}) appeared in v2 corpus"
            )
        if q.get("type") == "unanswerable_near":
            shared = set(content_tokens(q_text)) & all_corpus_tokens
            assert len(shared) >= 2, f"unanswerable_near {q['id']} shares too few corpus tokens"


def test_v2_committed_results_and_report_sync() -> None:
    v2_results_dir = ROOT / "evals" / "results" / "v2"
    v2_cal_dir = ROOT / "evals" / "calibrations" / "v2"
    hashing_slug = "hashing-hashing-cjk-v1-512-redact-0-qp-e3b0c442-dp-e3b0c442"
    qwen_slug = "openai-compatible-qwen3-embedding-8b-64b933495768-4096h-redact-0-qp-e1e90dad-dp-e3b0c442"

    for slug in (hashing_slug, qwen_slug):
        res_path = v2_results_dir / f"{slug}.json"
        cal_path = v2_cal_dir / f"{slug}.json"
        assert res_path.is_file(), f"Missing v2 result file: {res_path}"
        assert cal_path.is_file(), f"Missing v2 calibration file: {cal_path}"
        res_doc = json.loads(res_path.read_text(encoding="utf-8"))
        cal_doc = json.loads(cal_path.read_text(encoding="utf-8"))
        assert res_doc["dataset"] == "v2"
        assert cal_doc["dataset"] == "v2"
        assert res_doc["questions"]["total"] >= 300
        selected_mode = res_doc["selected_mode"]
        by_type = res_doc["modes"][selected_mode]["test"]["by_type"]
        assert set(by_type.keys()) == REQUIRED_TYPES

    assert run_report(root=ROOT, check=True) == 0
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")
    results_readme_text = (ROOT / "evals" / "results" / "README.md").read_text(encoding="utf-8")
    assert "| `v1` |" in readme_text
    assert "| `v2` |" in readme_text
    assert "| `v1` |" in results_readme_text
    assert "| `v2` |" in results_readme_text


async def test_v2_dataset_path_resolution_and_seed_only_acl_filtering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.acl import Principal
    from app.config import get_settings, reset_settings_cache
    from app.db import dispose_engine, get_sessionmaker
    from app.models import Document
    from app.rag.embedder import build_embedder
    from app.rag.retrieve import search_knowledge
    from sqlalchemy import select

    # 1. Even when docker-compose v1 defaults are set in CORPUS_DIR / QUESTIONS_PATH,
    # --dataset v2 resolves to evals/data/v2/corpus and evals/data/v2/questions.jsonl.
    monkeypatch.setenv("CORPUS_DIR", "/opt/lab-agent/evals/data/v1/corpus")
    monkeypatch.setenv("QUESTIONS_PATH", "/opt/lab-agent/evals/data/v1/questions.jsonl")
    reset_settings_cache()
    try:
        s = get_settings()
        assert _resolve_corpus_dir(s, "v2") == V2_CORPUS_DIR
        assert _resolve_questions_path(s, "v2") == V2_QUESTIONS_PATH
    finally:
        monkeypatch.delenv("CORPUS_DIR", raising=False)
        monkeypatch.delenv("QUESTIONS_PATH", raising=False)
        reset_settings_cache()

    # 2. _prepare ingests seed_only into source="eval" under a separate owner and gives
    # the evaluation user role="member", so SQL ACL filtering actively blocks seed_only docs
    # for the evaluation user while an admin principal can retrieve them from source="eval".
    settings = get_settings()
    user = await _prepare(settings, corpus_dir=V2_CORPUS_DIR)
    assert user.role == "member"

    embedder = build_embedder(settings)
    maker = get_sessionmaker()
    try:
        async with maker() as session:
            eval_seed_docs = list(
                await session.scalars(
                    select(Document).where(
                        Document.source == "eval",
                        Document.doc_key.in_(
                            ["private-mentor-eval", "restricted-patent-draft"]
                        ),
                    )
                )
            )
            assert len(eval_seed_docs) == 2
            for sdoc in eval_seed_docs:
                assert sdoc.owner_id != user.id
                assert sdoc.visibility in {"private", "restricted"}

            query = "2026秋季导师私下转博评估备忘录中推迟转博答辩的博士生"
            member_bundle = await search_knowledge(
                session,
                settings=settings,
                embedder=embedder,
                user=Principal(id=user.id, role=user.role),
                query=query,
                mode="dense",
                source="eval",
                limit=None,
            )
            member_keys = {str(h["key"]).split("#", 1)[0] for h in member_bundle.hits}
            assert "private-mentor-eval" not in member_keys
            assert "restricted-patent-draft" not in member_keys

            admin_bundle = await search_knowledge(
                session,
                settings=settings,
                embedder=embedder,
                user=Principal(id=user.id, role="admin"),
                query=query,
                mode="dense",
                source="eval",
                limit=None,
            )
            admin_keys = {str(h["key"]).split("#", 1)[0] for h in admin_bundle.hits}
            assert "private-mentor-eval" in admin_keys
            assert "restricted-patent-draft" in admin_keys
    finally:
        await embedder.aclose()
        await dispose_engine()

