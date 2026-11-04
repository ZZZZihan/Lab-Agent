"""LangGraph tool loop. Generation runs only after the evidence check passes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.config import Settings
from app.llm import CAPTURED, LLM, best_sentence
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


class Runtime(TypedDict):
    session: AsyncSession
    user: User
    settings: Settings
    embedder: Embedder
    llm: LLM
    emit: Emit
    request_id: str | None


_runtime: ContextVar[Runtime] = ContextVar("lab_agent_runtime")

MEETING_WORDS = ("组会", "研讨", "会议室", "轮值", "提纲", "例会")
PROJECT_WORDS = ("项目", "代号", "里程碑", "赫利俄斯", "流明", "hx-17")
PAPER_WORDS = ("论文", "arxiv", "预印", "笔记")
DETAIL_WORDS = ("全文", "详细说明", "完整内容")


def detect_intent(question: str) -> str:
    folded = question.lower()
    if any(word in folded for word in MEETING_WORDS):
        return "meeting"
    if any(word in folded for word in PROJECT_WORDS):
        return "project"
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
        if intent == "project":
            return {"name": "query_lab_projects", "args": {"query": question, "k": 12}}
        if intent == "meeting":
            return {"name": "query_meetings", "args": {"query": question, "k": 12}}
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
        score_min=0.99 if settings.evidence_score_min is None else float(settings.evidence_score_min),
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
    neighbors = [_public_citation(hit, index) for index, hit in enumerate(decision["neighbors"], start=1)]
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
    decision: Decision = state["decision"]  # type: ignore[assignment]
    question = str(state.get("question") or "")
    redacted_question, redaction_count = redact_text(question)
    citations = []
    blocks: list[str] = []
    evidence_for_model: list[str] = []
    for index, hit in enumerate(decision["citations"], start=1):
        citations.append(_public_citation(hit, index))
        redacted_chunk, count = redact_text(str(hit["content"]))
        redaction_count += count
        sentence = best_sentence(redacted_question, redacted_chunk)
        heading = str(hit.get("heading") or "")
        title = str(hit.get("title") or "")
        where = f"《{title}》" + (f"·{heading}" if heading else "")
        blocks.append(f"[{index}] {sentence}（{where}）")
        evidence_for_model.append(f"{where}\n{redacted_chunk}")
    await runtime["emit"]({"type": "citations", "items": citations, "paper_status": state.get("paper_status")})
    llm = runtime["llm"]
    # The capture log is the redacted evidence an external model would receive,
    # even when the local fallback streams an extractive sentence instead.
    if runtime["settings"].llm_capture:
        CAPTURED.append(redacted_question + "\n" + "\n".join(evidence_for_model))
    if llm.provider == "fake":
        composed = "根据实验室文档中可核对的段落，回答如下。\n" + "\n".join(blocks)
        composed += "\n以上内容只复述检索到的原文，没有使用这些段落之外的信息。"
        stream_input = [composed]
    else:
        stream_input = evidence_for_model
    parts: list[str] = []
    async for token in llm.generate(redacted_question, stream_input):
        parts.append(token)
        await runtime["emit"]({"type": "token", "text": token})
    prompt_tokens, completion_tokens, estimated = llm.usage()
    await write_audit(
        settings=runtime["settings"],
        actor_id=runtime["user"].id,
        action="llm_call",
        resource_type="chat",
        request_id=runtime["request_id"],
        detail={
            "model": llm.model,
            "provider": llm.provider,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "token_estimate": estimated,
            "redaction_count": redaction_count,
            "citation_count": len(citations),
        },
    )
    return {
        "answer": "".join(parts),
        "redactions": redaction_count,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated": estimated,
            "model": llm.model,
        },
        "citations": citations,
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
    graph.add_edge("tools", "agent")
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
    }
