"""Spec §9.2.D 30-item Lab Assets QA evaluation suite."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.importer.pii import assert_no_pii
from httpx import AsyncClient
from tests.test_proposal_coldstart import (
    ADMIN_EMAIL,
    INITIAL_PW,
    _login_headers,
    _seed_admin,
    build_synthetic_proposal_docx,
)

DATASET_PATH = Path(__file__).resolve().parents[2] / "evals" / "datasets" / "lab_qa.jsonl"

EXPECTED_CATEGORIES = {
    "pi_lookup",
    "status_enum",
    "member_specialty",
    "semantic_match",
    "achievement_kind",
    "format_reuse",
    "out_of_kb_refusal",
}


def _load_lab_qa_dataset() -> list[dict[str, Any]]:
    assert DATASET_PATH.is_file(), f"Missing dataset file: {DATASET_PATH}"
    raw_lines = [
        line.strip()
        for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(raw_lines) == 30, f"Expected 30 JSONL items, got {len(raw_lines)}"
    items: list[dict[str, Any]] = []
    for line in raw_lines:
        assert_no_pii(line)
        row = json.loads(line)
        assert isinstance(row, dict)
        for key in (
            "id",
            "category",
            "question",
            "expect_refused",
            "expected_facts",
            "forbidden_facts",
        ):
            assert key in row, f"Missing required field {key} in {row}"
        items.append(row)
    return items


def _parse_sse_events(raw_sse: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in raw_sse.split("\n\n"):
        if "data:" not in block:
            continue
        data_part = block.split("data:", 1)[1].strip()
        if not data_part:
            continue
        events.append(json.loads(data_part))
    return events


async def test_lab_qa_30_item_eval_suite(client: AsyncClient) -> None:
    dataset = _load_lab_qa_dataset()
    categories_seen = {str(item["category"]) for item in dataset}
    assert categories_seen == EXPECTED_CATEGORIES

    await _seed_admin()
    headers = await _login_headers(client, ADMIN_EMAIL)
    raw_docx = build_synthetic_proposal_docx()

    parse_resp = await client.post(
        "/api/v1/admin/imports/proposal",
        files={"file": ("proposal.docx", raw_docx, "application/octet-stream")},
        headers=headers,
    )
    assert parse_resp.status_code == 200, parse_resp.text
    job_id = parse_resp.json()["job_id"]

    prov_resp = await client.post(
        f"/api/v1/admin/imports/{job_id}/provision",
        json={"initial_password": INITIAL_PW},
        headers=headers,
    )
    assert prov_resp.status_code == 200, prov_resp.text

    latencies: list[float] = []
    routed_count = 0
    positive_routed_count = 0
    positive_cited_count = 0
    positive_total = 0

    category_fact_hits: dict[str, list[float]] = {cat: [] for cat in EXPECTED_CATEGORIES}
    refusal_verified_count = 0
    refusal_total = 0

    for item in dataset:
        qid = str(item["id"])
        cat = str(item["category"])
        question = str(item["question"])
        expect_refused = bool(item["expect_refused"])
        expected_facts = [str(f) for f in item["expected_facts"]]
        forbidden_facts = [str(f) for f in item["forbidden_facts"]]

        t0 = time.perf_counter()
        resp = await client.post(
            "/api/v1/chat/stream",
            json={"message": question},
            headers=headers,
        )
        elapsed_s = time.perf_counter() - t0
        latencies.append(elapsed_s)
        assert resp.status_code == 200, f"{qid} HTTP failed: {resp.text}"

        events = _parse_sse_events(resp.text)
        tool_events = [e for e in events if e.get("type") == "tool"]
        used_lab_tool = any(e.get("name") == "query_lab_assets" for e in tool_events)
        if used_lab_tool:
            routed_count += 1

        answer_tokens = [
            str(e.get("text") or "") for e in events if e.get("type") == "token"
        ]
        answer_text = "".join(answer_tokens)
        assert_no_pii(answer_text)

        citations_event = next(
            (e for e in events if e.get("type") == "citations"),
            {"items": []},
        )
        citation_items: list[dict[str, Any]] = list(citations_event.get("items") or [])
        excerpts_text = " ".join(str(c.get("excerpt") or "") for c in citation_items)
        combined_text = f"{answer_text}\n{excerpts_text}"

        for forbidden in forbidden_facts:
            assert forbidden not in combined_text, (
                f"{qid} leaked forbidden fact {forbidden!r} in output: {combined_text!r}"
            )

        if expect_refused:
            refusal_total += 1
            done_event = next((e for e in events if e.get("type") == "done"), {})
            # Verify out-of-KB question either triggers refusal or has zero forbidden facts in retrieved evidence
            ungrounded = bool(done_event.get("refused")) or all(
                f not in combined_text for f in forbidden_facts
            )
            if ungrounded:
                refusal_verified_count += 1
        else:
            positive_total += 1
            if used_lab_tool:
                positive_routed_count += 1

            has_profile_citation = bool(citation_items) and any(
                str(c.get("doc_key") or "").startswith("lab-profile-")
                for c in citation_items
            )
            assert has_profile_citation, (
                f"{qid} missing lab-profile-* citation: {citation_items}"
            )
            positive_cited_count += 1

            matched_facts = [f for f in expected_facts if f in answer_text]
            recall = (
                len(matched_facts) / len(expected_facts) if expected_facts else 1.0
            )
            assert recall == 1.0, (
                f"{qid} ({cat}) missed facts {set(expected_facts) - set(matched_facts)} "
                f"in answer: {answer_text!r}"
            )
            category_fact_hits[cat].append(recall)

    assert positive_total == 27
    assert refusal_total == 3

    overall_routing_rate = routed_count / len(dataset)
    positive_routing_rate = positive_routed_count / positive_total
    positive_citation_rate = positive_cited_count / positive_total

    assert overall_routing_rate >= 0.95
    assert positive_routing_rate == 1.0
    assert positive_citation_rate == 1.0
    assert refusal_verified_count == refusal_total

    for strict_cat in ("pi_lookup", "status_enum", "achievement_kind", "format_reuse"):
        scores = category_fact_hits[strict_cat]
        assert len(scores) >= 4
        assert sum(scores) / len(scores) == 1.0

    member_scores = category_fact_hits["member_specialty"]
    assert len(member_scores) == 5
    assert sum(member_scores) / len(member_scores) >= 0.95

    sem_scores = category_fact_hits["semantic_match"]
    assert len(sem_scores) == 4
    assert sum(sem_scores) / len(sem_scores) == 1.0

    sorted_lat = sorted(latencies)
    p95_idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.95))
    p95_latency = sorted_lat[p95_idx]
    assert p95_latency <= 2.5, f"P95 latency {p95_latency:.3f}s exceeded 2.5s"
