"""LangGraph tool loop. Generation runs only after the evidence check passes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.config import Settings
from app.llm import (
    LLM,
    Evidence,
    LLMUnavailable,
    Usage,
    check_citations,
)
from app.models import User
from app.rag.embedder import Embedder
from app.rag.redact import redact_text
from app.rag.rrf import Decision, Hit, decide
from app.tools import (
    fetch_document,
    query_lab_projects,
    query_meetings,
    search_knowledge_base,
    search_papers,
)

try:
    from app.rag.embedder import EmbeddingError  # type: ignore[attr-defined]
except ImportError:

    class EmbeddingError(Exception):  # type: ignore[no-redef]
        code: str

        def __init__(self, code: str = "embedding_unavailable") -> None:
            super().__init__(code)
            self.code = code


Emit = Callable[[dict[str, object]], Awaitable[None]]


class LoopState(TypedDict, total=False):
    question: str
    steps: int
    trace: list[dict[str, object]]
    pending: dict[str, object] | None
    hits: list[Hit]
    coverage: float
    elapsed_ms: float
    mode: str
    paper_status: str | None
    fetched_id: str | None
    decision: Decision
    answer: str
    citations: list[dict[str, object]]
    usage: dict[str, object]
    redactions: int
    llm_error: str | None
    embedding_error: str | None


class Runtime(TypedDict):
    session: AsyncSession
    user: User
    settings: Settings
    embedder: Embedder
    llm: LLM
    emit: Emit
    request_id: str | None


_runtime: ContextVar[Runtime] = ContextVar("lab_agent_runtime")

PAPER_WORDS = ("论文", "arxiv", "预印", "笔记")
DETAIL_WORDS = ("全文", "详细说明", "完整内容")


def detect_intent(question: str) -> str:
    folded = question.lower()
    if any(word in folded for word in PAPER_WORDS):
        return "paper"
    return "knowledge"


def heuristic_action(state: LoopState, max_steps: int) -> dict[str, object] | None:
    if int(state.get("steps") or 0) >= max_steps:
        return None
    trace = [str(item.get("tool")) for item in state.get("trace") or []]
    question = str(state.get("question") or "")
    intent = detect_intent(question)
    searched = {"search_knowledge_base", "query_lab_projects", "query_meetings"} & set(trace)
    if not searched:
        return {"name": "search_knowledge_base", "args": {"query": question, "k": 12}}
    if intent == "paper" and "search_papers" not in trace:
        return {"name": "search_papers", "args": {"query": question}}
    hits = state.get("hits") or []
    if hits and any(word in question for word in DETAIL_WORDS) and "fetch_document" not in trace:
        return {
            "name": "fetch_document",
            "args": {"document_id": hits[0]["document_id"]},
        }
    return None


async def _agent_node(state: LoopState) -> dict[str, object]:
    runtime = _runtime.get()
    pending = heuristic_action(state, runtime["settings"].agent_max_steps)
    return {"pending": pending}


async def _tools_node(state: LoopState) -> dict[str, object]:
    runtime = _runtime.get()
    pending = state.get("pending") or {}
    name = str(pending.get("name"))
    raw_args = pending.get("args")
    args = dict(raw_args) if isinstance(raw_args, dict) else {}
    await runtime["emit"]({"type": "status", "phase": name})
    hits = list(state.get("hits") or [])
    coverage = float(state.get("coverage") or 0.0)
    elapsed = float(state.get("elapsed_ms") or 0.0)
    mode = str(state.get("mode") or runtime["settings"].retrieval_mode)
    paper_status = state.get("paper_status")
    fetched_id = state.get("fetched_id")
    status = "ok"
    try:
        if name == "search_knowledge_base":
            found = await search_knowledge_base(
                runtime["session"],
                settings=runtime["settings"],
                embedder=runtime["embedder"],
                user=runtime["user"],
                query=str(args.get("query") or ""),
                k=int(args.get("k") or 12),
                request_id=runtime["request_id"],
            )
            hits = found
            if found:
                coverage = float(found[0].get("_coverage") or 0.0)  # type: ignore[arg-type]
                elapsed = float(found[0].get("_elapsed_ms") or 0.0)  # type: ignore[arg-type]
                mode = str(found[0].get("_mode") or mode)
        elif name == "query_lab_projects":
            result = await query_lab_projects(
                runtime["session"],
                settings=runtime["settings"],
                embedder=runtime["embedder"],
                user=runtime["user"],
                query=str(args.get("query") or ""),
                k=int(args.get("k") or 12),
                request_id=runtime["request_id"],
            )
            raw_hits = result["hits"]
            hits = list(raw_hits) if isinstance(raw_hits, list) else []
            if hits:
                coverage = float(hits[0].get("_coverage") or 0.0)  # type: ignore[arg-type]
                elapsed = float(hits[0].get("_elapsed_ms") or 0.0)  # type: ignore[arg-type]
                mode = str(hits[0].get("_mode") or mode)
        elif name == "query_meetings":
            result = await query_meetings(
                runtime["session"],
                settings=runtime["settings"],
                embedder=runtime["embedder"],
                user=runtime["user"],
                query=str(args.get("query") or ""),
                k=int(args.get("k") or 12),
                request_id=runtime["request_id"],
            )
            raw_hits = result["hits"]
            hits = list(raw_hits) if isinstance(raw_hits, list) else []
            if hits:
                coverage = float(hits[0].get("_coverage") or 0.0)  # type: ignore[arg-type]
                elapsed = float(hits[0].get("_elapsed_ms") or 0.0)  # type: ignore[arg-type]
                mode = str(hits[0].get("_mode") or mode)
        elif name == "search_papers":
            result = await search_papers(
                settings=runtime["settings"],
                user=runtime["user"],
                query=str(args.get("query") or ""),
                request_id=runtime["request_id"],
            )
            paper_status = str(result.get("status"))
            status = paper_status or "not_configured"
        elif name == "fetch_document":
            fetched = await fetch_document(
                runtime["session"],
                settings=runtime["settings"],
                user=runtime["user"],
                document_id=str(args.get("document_id") or ""),
                request_id=runtime["request_id"],
            )
            if fetched is None:
                status = "not_found"
            else:
                fetched_id = str(fetched["document_id"])
                status = "ok"
        else:
            status = "unknown_tool"
    except EmbeddingError as exc:
        err_code = str(getattr(exc, "code", "embedding_unavailable"))
        await runtime["emit"]({"type": "error", "code": "embedding_unavailable"})
        await write_audit(
            settings=runtime["settings"],
            actor_id=runtime["user"].id,
            action="retrieval",
            resource_type="chunk",
            request_id=runtime["request_id"],
            detail={"status": "error", "error": err_code},
        )
        return {
            "embedding_error": err_code,
            "pending": None,
            "answer": "",
            "citations": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }
    await write_audit(
        settings=runtime["settings"],
        actor_id=runtime["user"].id,
        action="tool_call",
        resource_type="tool",
        request_id=runtime["request_id"],
        detail={"tool": name, "status": status, "hit_count": len(hits)},
    )
    await runtime["emit"]({"type": "tool", "name": name, "status": status})
    trace = list(state.get("trace") or [])
    trace.append({"tool": name, "status": status})
    return {
        "trace": trace,
        "steps": int(state.get("steps") or 0) + 1,
        "pending": None,
        "hits": hits,
        "coverage": coverage,
        "elapsed_ms": elapsed,
        "mode": mode,
        "paper_status": paper_status,
        "fetched_id": fetched_id,
    }


def _route_after_tools(state: LoopState) -> str:
    if state.get("embedding_error"):
        return "end"
    return "agent"


def _route_after_agent(state: LoopState) -> str:
    if state.get("pending"):
        return "tools"
    return "grade"


def _route_after_grade(state: LoopState) -> str:
    decision = state.get("decision")
    if isinstance(decision, dict) and decision.get("refused"):
        return "refuse"
    return "generate"


async def _grade_node(state: LoopState) -> dict[str, object]:
    runtime = _runtime.get()
    settings = runtime["settings"]
    decision = decide(
        list(state.get("hits") or []),
        mode=str(state.get("mode") or settings.retrieval_mode),
        score_min=(
            0.99 if settings.evidence_score_min is None else float(settings.evidence_score_min)
        ),
        rare_coverage=float(state.get("coverage") or 0.0),
        rare_min=float(settings.rare_coverage_min or 0.0),
        margin=float(settings.cite_margin or 0.15),
        cite_k=int(settings.cite_k or 3),
    )
    public_hits = [_public_hit(hit) for hit in (state.get("hits") or [])[:8]]
    await runtime["emit"](
        {
            "type": "retrieval",
            "mode": state.get("mode"),
            "coverage": round(float(state.get("coverage") or 0.0), 4),
            "elapsed_ms": round(float(state.get("elapsed_ms") or 0.0), 2),
            "hits": public_hits,
            "refused": decision["refused"],
        }
    )
    return {"decision": decision}


async def _refuse_node(state: LoopState) -> dict[str, object]:
    runtime = _runtime.get()
    decision = state["decision"]
    assert isinstance(decision, dict)
    await write_audit(
        settings=runtime["settings"],
        actor_id=runtime["user"].id,
        action="answer_refused",
        resource_type="chat",
        request_id=runtime["request_id"],
        detail={
            "reason": str(decision["reason"]),
            "score": round(float(decision["score"]), 4),
            "cosine": round(float(decision["cosine"]), 4),
            "coverage": round(float(decision["coverage"]), 4),
            "threshold": float(runtime["settings"].evidence_score_min or 0.0),
        },
    )
    neighbors = [
        _public_citation(hit, index) for index, hit in enumerate(decision["neighbors"], start=1)
    ]
    await runtime["emit"](
        {
            "type": "refusal",
            "reason": decision["reason"],
            "score": decision["score"],
            "cosine": decision["cosine"],
            "coverage": decision["coverage"],
            "threshold": runtime["settings"].evidence_score_min,
            "neighbors": neighbors,
            "paper_status": state.get("paper_status"),
        }
    )
    return {"answer": "", "redactions": 0, "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


async def _generate_node(state: LoopState) -> dict[str, object]:
    runtime = _runtime.get()
    settings = runtime["settings"]
    decision: Decision = state["decision"]  # type: ignore[assignment]
    question = str(state.get("question") or "")
    if settings.llm_redact:
        outbound_question, redaction_count = redact_text(question)
    else:
        outbound_question = question
        redaction_count = 0

    citations: list[dict[str, object]] = []
    evidence_items: list[Evidence] = []
    for index, hit in enumerate(decision["citations"], start=1):
        citations.append(_public_citation(hit, index))
        raw_chunk = str(hit["content"])
        if settings.llm_redact:
            outbound_chunk, count = redact_text(raw_chunk)
            redaction_count += count
        else:
            outbound_chunk = raw_chunk
        evidence_items.append(
            Evidence(
                index=index,
                title=str(hit.get("title") or ""),
                heading=str(hit.get("heading") or ""),
                text=outbound_chunk,
            )
        )

    await runtime["emit"](
        {"type": "citations", "items": citations, "paper_status": state.get("paper_status")}
    )

    llm = runtime["llm"]
    usage = Usage()
    parts: list[str] = []
    try:
        async for token in llm.generate(outbound_question, evidence_items, usage):
            parts.append(token)
            await runtime["emit"]({"type": "token", "text": token})
    except LLMUnavailable as exc:
        await runtime["emit"]({"type": "error", "code": "llm_unavailable"})
        await write_audit(
            settings=settings,
            actor_id=runtime["user"].id,
            action="llm_call",
            resource_type="chat",
            request_id=runtime["request_id"],
            detail={
                "status": "error",
                "error": exc.code,
                "model": llm.model,
                "provider": llm.provider,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "token_estimate": usage.estimated,
                "redaction_count": redaction_count,
                "redact": bool(settings.llm_redact),
                "citation_count": 0,
            },
        )
        return {
            "answer": "",
            "llm_error": exc.code,
            "redactions": redaction_count,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "estimated": usage.estimated,
                "model": llm.model,
            },
            "citations": [],
        }

    answer = "".join(parts)
    check = check_citations(answer, len(citations))
    await runtime["emit"](
        {
            "type": "citation_check",
            "used": check.used,
            "invalid": check.invalid,
            "uncited": check.uncited,
        }
    )

    if check.used:
        used_set = set(check.used)
        final_citations = [
            item
            for item in citations
            if isinstance(idx := item.get("index"), int) and idx in used_set
        ]
    else:
        final_citations = list(citations)

    updated_decision: Decision = decision
    if check.declined:
        updated_decision = {**decision, "refused": True, "reason": "model_declined"}
        final_citations = []
        await write_audit(
            settings=settings,
            actor_id=runtime["user"].id,
            action="answer_refused",
            resource_type="chat",
            request_id=runtime["request_id"],
            detail={
                "reason": "model_declined",
                "score": round(float(decision["score"]), 4),
                "cosine": round(float(decision["cosine"]), 4),
                "coverage": round(float(decision["coverage"]), 4),
                "threshold": float(settings.evidence_score_min or 0.0),
            },
        )
        neighbors = [
            _public_citation(hit, index)
            for index, hit in enumerate(decision["neighbors"], start=1)
        ]
        await runtime["emit"](
            {
                "type": "refusal",
                "reason": "model_declined",
                "score": decision["score"],
                "cosine": decision["cosine"],
                "coverage": decision["coverage"],
                "threshold": settings.evidence_score_min,
                "neighbors": neighbors,
                "paper_status": state.get("paper_status"),
            }
        )

    await write_audit(
        settings=settings,
        actor_id=runtime["user"].id,
        action="llm_call",
        resource_type="chat",
        request_id=runtime["request_id"],
        detail={
            "status": "ok",
            "model": llm.model,
            "provider": llm.provider,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "token_estimate": usage.estimated,
            "redaction_count": redaction_count,
            "redact": bool(settings.llm_redact),
            "citation_count": len(final_citations),
            "citations_used": check.used,
            "citations_invalid": check.invalid,
            "uncited": check.uncited,
            "declined": check.declined,
        },
    )
    return {
        "answer": answer,
        "decision": updated_decision,
        "redactions": redaction_count,
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "estimated": usage.estimated,
            "model": llm.model,
        },
        "citations": final_citations,
    }


def _public_hit(hit: Hit) -> dict[str, object]:
    return {
        "chunk_id": hit["chunk_id"],
        "document_id": hit["document_id"],
        "title": hit.get("title"),
        "heading": hit.get("heading"),
        "score": round(float(hit.get("cosine") or 0.0), 4),
    }


def _public_citation(hit: Hit, index: int) -> dict[str, object]:
    return {
        "index": index,
        "chunk_id": hit["chunk_id"],
        "document_id": hit["document_id"],
        "doc_key": hit.get("doc_key"),
        "chunk_index": hit.get("chunk_index"),
        "title": hit.get("title"),
        "heading": hit.get("heading"),
        "collection": hit.get("collection"),
        "excerpt": str(hit.get("content") or "")[:280],
        "score": round(float(hit.get("cosine") or 0.0), 4),
    }


def build_graph() -> object:
    graph = StateGraph(LoopState)
    graph.add_node("agent", _agent_node)
    graph.add_node("tools", _tools_node)
    graph.add_node("grade", _grade_node)
    graph.add_node("generate", _generate_node)
    graph.add_node("refuse", _refuse_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", _route_after_agent, {"tools": "tools", "grade": "grade"})
    graph.add_conditional_edges("tools", _route_after_tools, {"agent": "agent", "end": END})
    graph.add_conditional_edges(
        "grade", _route_after_grade, {"generate": "generate", "refuse": "refuse"}
    )
    graph.add_edge("generate", END)
    graph.add_edge("refuse", END)
    return graph.compile()


_COMPILED = build_graph()


async def run_agent(
    *,
    session: AsyncSession,
    user: User,
    settings: Settings,
    embedder: Embedder,
    llm: LLM,
    question: str,
    emit: Emit,
    request_id: str | None,
) -> dict[str, object]:
    token = _runtime.set(
        {
            "session": session,
            "user": user,
            "settings": settings,
            "embedder": embedder,
            "llm": llm,
            "emit": emit,
            "request_id": request_id,
        }
    )
    try:
        result = await _COMPILED.ainvoke(  # type: ignore[attr-defined]
            {"question": question, "steps": 0, "trace": [], "hits": [], "coverage": 0.0}
        )
    finally:
        _runtime.reset(token)
    decision = result.get("decision") or {}
    return {
        "answer": result.get("answer") or "",
        "refused": bool(decision.get("refused")),
        "decision": decision,
        "citations": result.get("citations") or [],
        "usage": result.get("usage") or {},
        "redactions": int(result.get("redactions") or 0),
        "trace": result.get("trace") or [],
        "paper_status": result.get("paper_status"),
        "llm_error": result.get("llm_error"),
        "embedding_error": result.get("embedding_error"),
    }
