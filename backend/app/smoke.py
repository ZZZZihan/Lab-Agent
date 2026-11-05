"""End-to-end smoke test suite via the Next.js session/backend proxy (`python -m app.smoke`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import string
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

DEMO_ACCOUNTS: dict[str, tuple[str, str]] = {
    "admin": ("admin@lab.local", "LabAgent-Demo-2026-Admin"),
    "linke": ("member@lab.local", "LabAgent-Demo-2026-Member"),
    "zhouning": ("zhou@lab.local", "LabAgent-Demo-2026-Zhou"),
    "viewer": ("viewer@lab.local", "LabAgent-Demo-2026-Viewer"),
}


@dataclass(frozen=True)
class StepResult:
    step: str
    status: str
    elapsed_ms: float
    detail: str = ""


def parse_sse_events(raw: str) -> list[dict[str, object]]:
    """Parse an SSE response payload into a list of JSON event objects."""
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    events: list[dict[str, object]] = []
    for block in normalized.split("\n\n"):
        if not block.strip():
            continue
        event_name: str | None = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            continue
        parsed = json.loads(payload)
        if isinstance(parsed, dict):
            if "type" not in parsed and event_name:
                parsed["type"] = event_name
            events.append(parsed)
    return events


def _format_table(results: list[StepResult]) -> str:
    headers = ("Step", "Result", "Time (ms)", "Detail")
    rows = [
        (r.step, r.status, f"{r.elapsed_ms:.1f}", r.detail)
        for r in results
    ]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]
    sep = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    head = "| " + " | ".join(headers[i].ljust(widths[i]) for i in range(len(headers))) + " |"
    lines = [sep, head, sep]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                row[i].rjust(widths[i]) if i == 2 else row[i].ljust(widths[i])
                for i in range(len(headers))
            )
            + " |"
        )
    lines.append(sep)
    return "\n".join(lines)


async def run_smoke(
    *,
    web_url: str,
    api_url: str,
    offline: bool = False,
    poll_timeout: float = 180.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[int, str]:
    web_base = web_url.rstrip("/")
    api_base = api_url.rstrip("/")
    results: list[StepResult] = []
    nonce = "".join(secrets.choice(string.ascii_lowercase) for _ in range(8))
    rotation_days = 17
    s4_doc_id: str | None = None
    s5_question = f"设备间{nonce}的门禁多久轮换一次？"
    s6_question = "火星基地的氧气配额是多少？"

    timeout = httpx.Timeout(timeout=120.0, connect=10.0)
    api_client = httpx.AsyncClient(base_url=api_base, timeout=timeout, transport=transport)
    clients: dict[str, httpx.AsyncClient] = {
        key: httpx.AsyncClient(base_url=web_base, timeout=timeout, transport=transport)
        for key in DEMO_ACCOUNTS
    }

    async def run_step(name: str, fn: Callable[[], Awaitable[str]]) -> bool:
        t0 = time.perf_counter()
        try:
            detail = await fn()
            elapsed = (time.perf_counter() - t0) * 1000.0
            results.append(StepResult(step=name, status="PASS", elapsed_ms=elapsed, detail=detail))
            return True
        except Exception as exc:
            elapsed = (time.perf_counter() - t0) * 1000.0
            results.append(
                StepResult(
                    step=name,
                    status="FAIL",
                    elapsed_ms=elapsed,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            return False

    async def s1_health() -> str:
        resp = await api_client.get("/api/v1/health")
        if resp.status_code != 200:
            raise AssertionError(f"health returned {resp.status_code}")
        body = resp.json()
        index_ok = bool(body.get("index_ok", body.get("embedding_index_ok")))
        for key, val in (
            ("database", body.get("database")),
            ("redis", body.get("redis")),
            ("index_ok", index_ok),
            ("calibrated", body.get("calibrated")),
        ):
            if val is not True:
                raise AssertionError(f"expected {key}=True, got {val!r}")
        fp = str(body.get("embedding_fingerprint") or "")
        if not offline:
            valid_prefix = fp.startswith("openai_compatible|lab-agent-qwen3-embed@") or fp.startswith(
                "openai_compatible|qwen3-embedding:8b@"
            )
            if not valid_prefix:
                raise AssertionError(f"unexpected real-mode fingerprint prefix: {fp}")
            if "|4096h|" not in fp:
                raise AssertionError(f"expected '|4096h|' in fingerprint: {fp}")
            if body.get("embedding_index") != "none":
                raise AssertionError(
                    f"expected embedding_index='none', got {body.get('embedding_index')!r}"
                )
            if body.get("llm_provider") != "openai_compatible":
                raise AssertionError(
                    f"expected llm_provider='openai_compatible', got {body.get('llm_provider')!r}"
                )
        return f"fp={fp}"

    async def s2_login() -> str:
        for key, (email, password) in DEMO_ACCOUNTS.items():
            client = clients[key]
            resp = await client.post(
                "/api/session",
                json={"email": email, "password": password},
            )
            if resp.status_code != 200:
                raise AssertionError(f"login failed for {key} ({resp.status_code}): {resp.text}")
            has_cookie = "la_session" in client.cookies or "la_session" in resp.cookies
            if not has_cookie:
                raise AssertionError(f"la_session cookie missing for {key}")
        return "4 accounts logged in"

    async def s3_viewer_upload() -> str:
        resp = await clients["viewer"].post(
            "/api/backend/documents",
            data={
                "title": f"viewer-denied-{nonce}",
                "text": "## 权限检查\n查看者不能上传文档。",
                "collection": "knowledge",
                "visibility": "lab",
            },
        )
        if resp.status_code != 403:
            raise AssertionError(f"expected 403 for viewer upload, got {resp.status_code}")
        return "status=403"

    async def s4_member_upload() -> str:
        nonlocal s4_doc_id
        title = f"冒烟-{nonce}"
        content = f"## 设备间\n设备间{nonce}的门禁轮换周期是 {rotation_days} 天。"
        resp = await clients["linke"].post(
            "/api/backend/documents",
            data={
                "title": title,
                "text": content,
                "collection": "knowledge",
                "visibility": "lab",
            },
        )
        if resp.status_code != 202:
            raise AssertionError(f"expected 202 for member upload, got {resp.status_code}: {resp.text}")
        payload = resp.json()
        doc_id = str(payload.get("id") or "")
        if not doc_id:
            raise AssertionError("upload response missing document id")
        s4_doc_id = doc_id

        deadline = time.monotonic() + poll_timeout
        while time.monotonic() < deadline:
            list_resp = await clients["linke"].get("/api/backend/documents")
            if list_resp.status_code != 200:
                raise AssertionError(f"list documents returned {list_resp.status_code}")
            docs = list_resp.json().get("documents") or []
            matched = next((d for d in docs if isinstance(d, dict) and d.get("id") == doc_id), None)
            if matched is not None:
                status = matched.get("status")
                if status == "ready":
                    return f"id={doc_id} status=ready"
                if status == "failed":
                    raise AssertionError(
                        f"document indexing failed: error_code={matched.get('error_code')!r}"
                    )
            await asyncio.sleep(0.5)
        raise TimeoutError(f"document {doc_id} did not become ready within {poll_timeout}s")

    async def s5_cited_qa() -> str:
        if not s4_doc_id:
            raise AssertionError("S4 document id unavailable")
        resp = await clients["linke"].post(
            "/api/backend/chat/stream",
            json={"message": s5_question},
        )
        if resp.status_code != 200:
            raise AssertionError(f"chat/stream returned {resp.status_code}: {resp.text}")
        events = parse_sse_events(resp.text)
        citations_ev = next((e for e in events if e.get("type") == "citations"), None)
        if citations_ev is None:
            raise AssertionError(f"missing citations event in {[e.get('type') for e in events]}")
        items = citations_ev.get("items")
        if not isinstance(items, list) or not any(
            isinstance(item, dict) and item.get("document_id") == s4_doc_id for item in items
        ):
            raise AssertionError(f"citations did not include S4 document {s4_doc_id}")
        done_ev = next((e for e in events if e.get("type") == "done"), None)
        if done_ev is None or done_ev.get("refused") is not False:
            raise AssertionError(f"expected done.refused=False, got {done_ev!r}")
        answer_text = "".join(
            str(e.get("text") or "") for e in events if e.get("type") == "token"
        ).strip()
        if not answer_text:
            raise AssertionError("streamed answer is empty")
        if not offline:
            check_ev = next((e for e in events if e.get("type") == "citation_check"), None)
            used = check_ev.get("used") if isinstance(check_ev, dict) else None
            if not isinstance(used, list) or not used:
                raise AssertionError(f"expected non-empty citation_check.used in real mode, got {check_ev!r}")
        return f"chars={len(answer_text)}"

    async def s6_refusal() -> str:
        resp = await clients["linke"].post(
            "/api/backend/chat/stream",
            json={"message": s6_question},
        )
        if resp.status_code != 200:
            raise AssertionError(f"chat/stream returned {resp.status_code}: {resp.text}")
        events = parse_sse_events(resp.text)
        refusal_ev = next((e for e in events if e.get("type") == "refusal"), None)
        if refusal_ev is None:
            raise AssertionError(f"expected refusal event, got {[e.get('type') for e in events]}")
        done_ev = next((e for e in events if e.get("type") == "done"), None)
        if done_ev is None or done_ev.get("refused") is not True:
            raise AssertionError(f"expected done.refused=True, got {done_ev!r}")
        return f"reason={refusal_ev.get('reason')}"

    async def s7_isolation() -> str:
        groups_resp = await clients["linke"].get("/api/backend/groups")
        if groups_resp.status_code != 200:
            raise AssertionError(f"groups returned {groups_resp.status_code}")
        groups = groups_resp.json().get("groups") or []
        if not any(isinstance(g, dict) and g.get("slug") == "helios" for g in groups):
            raise AssertionError("expected group 'helios' in /groups response")

        zn_docs_resp = await clients["zhouning"].get("/api/backend/documents")
        if zn_docs_resp.status_code != 200:
            raise AssertionError(f"zhouning documents list returned {zn_docs_resp.status_code}")
        zn_docs = zn_docs_resp.json().get("documents") or []
        if any(
            isinstance(d, dict)
            and (d.get("title") == "项目赫利俄斯" or d.get("doc_key") == "project-helios")
            for d in zn_docs
        ):
            raise AssertionError("non-helios member zhouning saw '项目赫利俄斯' in document list")

        search_resp = await clients["zhouning"].post(
            "/api/backend/search",
            json={"query": "赫利俄斯 内部代号", "k": 8},
        )
        if search_resp.status_code != 200:
            raise AssertionError(f"search returned {search_resp.status_code}")
        hits = search_resp.json().get("hits") or []
        if any(isinstance(h, dict) and h.get("title") == "项目赫利俄斯" for h in hits):
            raise AssertionError("non-helios member zhouning search leaked restricted document '项目赫利俄斯'")

        admin_docs_resp = await clients["admin"].get("/api/backend/documents")
        if admin_docs_resp.status_code != 200:
            raise AssertionError(f"admin documents list returned {admin_docs_resp.status_code}")
        admin_docs = admin_docs_resp.json().get("documents") or []
        helios_doc = next(
            (
                d
                for d in admin_docs
                if isinstance(d, dict)
                and (d.get("title") == "项目赫利俄斯" or d.get("doc_key") == "project-helios")
            ),
            None,
        )
        if helios_doc is None or not helios_doc.get("id"):
            raise AssertionError("admin could not find '项目赫利俄斯' in document list")
        helios_id = str(helios_doc["id"])

        zn_get = await clients["zhouning"].get(f"/api/backend/documents/{helios_id}")
        if zn_get.status_code != 404:
            raise AssertionError(
                f"expected 404 for non-helios member zhouning GET helios, got {zn_get.status_code}"
            )

        lk_get = await clients["linke"].get(f"/api/backend/documents/{helios_id}")
        if lk_get.status_code != 200:
            raise AssertionError(
                f"expected 200 for helios group member linke GET helios, got {lk_get.status_code}"
            )
        return f"group=helios helios_id={helios_id} zn=404 lk=200"

    async def s8_audit() -> str:
        audit_resp = await clients["admin"].get("/api/backend/audit?limit=200")
        if audit_resp.status_code != 200:
            raise AssertionError(f"audit returned {audit_resp.status_code}")
        payload = audit_resp.json()
        events = payload.get("events") or []
        actions = {str(ev.get("action")) for ev in events if isinstance(ev, dict)}
        required = {
            "login",
            "document_upload",
            "document_indexed",
            "retrieval",
            "answer_refused",
            "permission_denied",
        }
        if not offline:
            required.add("llm_call")
        missing = sorted(required - actions)
        if missing:
            raise AssertionError(f"audit missing required actions: {missing} (found: {sorted(actions)})")

        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (nonce, "火星基地", s5_question):
            if forbidden in serialized:
                raise AssertionError(f"audit payload leaked raw query/content token: {forbidden!r}")
        return f"events={len(events)} actions={len(actions)}"

    async def s9_cleanup() -> str:
        nonlocal s4_doc_id
        if not s4_doc_id:
            raise AssertionError("S4 document id unavailable for cleanup")
        doc_id = s4_doc_id
        del_resp = await clients["linke"].delete(f"/api/backend/documents/{doc_id}")
        if del_resp.status_code != 200:
            raise AssertionError(f"expected 200 deleting {doc_id}, got {del_resp.status_code}")
        s4_doc_id = None
        return f"deleted={doc_id}"

    steps: list[tuple[str, Callable[[], Awaitable[str]]]] = [
        ("S1 health", s1_health),
        ("S2 login", s2_login),
        ("S3 viewer upload", s3_viewer_upload),
        ("S4 member upload", s4_member_upload),
        ("S5 cited QA", s5_cited_qa),
        ("S6 refusal", s6_refusal),
        ("S7 ACL isolation", s7_isolation),
        ("S8 audit trail", s8_audit),
        ("S9 cleanup", s9_cleanup),
    ]

    all_ok = True
    try:
        for step_name, step_fn in steps:
            ok = await run_step(step_name, step_fn)
            if not ok:
                all_ok = False
                break
        if not all_ok and s4_doc_id is not None:
            try:
                await clients["linke"].delete(f"/api/backend/documents/{s4_doc_id}")
            except Exception:
                pass
    finally:
        await api_client.aclose()
        for client in clients.values():
            await client.aclose()

    table = _format_table(results)
    return (0 if all_ok else 1), table


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Lab Agent end-to-end smoke checks.")
    parser.add_argument("--web", default="http://127.0.0.1:43123", help="Base URL of the web service")
    parser.add_argument("--api", default="http://127.0.0.1:43124", help="Base URL of the API service")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Offline mode (hashing embedder + fake LLM)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Timeout in seconds for document ingestion polling",
    )
    args = parser.parse_args(argv)
    code, output = asyncio.run(
        run_smoke(
            web_url=args.web,
            api_url=args.api,
            offline=args.offline,
            poll_timeout=args.timeout,
        )
    )
    print(output)
    return code


if __name__ == "__main__":
    sys.exit(main())
