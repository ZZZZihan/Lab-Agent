"""HTTP API for login, documents, grounded chat, and the audit trail."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.acl import Principal, acl_sql, can_read
from app.agent import run_agent
from app.audit import run_retention, write_audit
from app.auth import (
    Actor,
    authenticate_local,
    decode_unverified_for_logout,
    issue_token,
    revoke_token,
    verify_password,
)
from app.config import Settings
from app.db import get_session, get_sessionmaker
from app.llm import LLM
from app.models import Conversation, Document, DocumentACL, Message, User
from app.rag.embedder import Embedder
from app.rag.index_meta import check_index
from app.rag.ingest import ingest_text
from app.tools import search_knowledge_base

router = APIRouter(prefix="/api/v1")
bearer = HTTPBearer(auto_error=False)


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message


class LoginBody(BaseModel):
    email: str
    password: str


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=8, ge=1, le=20)
    collection: str | None = None


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None


class RetentionBody(BaseModel):
    days: int | None = None


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _embedder(request: Request) -> Embedder:
    return request.app.state.embedder  # type: ignore[no-any-return]


def _llm(request: Request) -> LLM:
    return request.app.state.llm  # type: ignore[no-any-return]


async def _actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Actor:
    if credentials is None:
        raise APIError(401, "unauthorized", "需要登录")
    settings = _settings(request)
    async with get_sessionmaker()() as session:
        try:
            return await authenticate_local(session, credentials.credentials, settings)
        except PermissionError as exc:
            raise APIError(401, "unauthorized", "登录已失效") from exc


async def _user(session: AsyncSession, actor: Actor) -> User:
    user = await session.get(User, actor.id)
    if user is None or not user.is_active:
        raise APIError(401, "unauthorized", "登录已失效")
    return user


def _client_ip(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host


@router.get("/health")
async def health(request: Request) -> dict[str, object]:
    settings = _settings(request)
    db_ok = False
    index_ok = False
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
            db_ok = True
            idx_state = await check_index(session, settings)
            index_ok = idx_state != "mismatch"
    except Exception:
        db_ok = False
        index_ok = False
    redis_ok = await request.app.state.limiter.ping()
    model_name = "hashing-cjk-v1" if settings.embedder == "hashing" else settings.embedding_model
    return {
        "status": "ok" if db_ok and redis_ok and index_ok else "degraded",
        "database": db_ok,
        "redis": redis_ok,
        "embedder": settings.embedder,
        "embedding_model": model_name,
        "embedding_dim": settings.embedding_dim,
        "embedding_storage": settings.embedding_storage,
        "embedding_fingerprint": settings.embedding_fingerprint,
        "embedding_index_ok": index_ok,
        "embedding_base_host": settings.embedding_base_host,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_base_host": settings.llm_base_host,
        "retrieval_mode": settings.retrieval_mode,
    }


@router.post("/auth/login")
async def login(body: LoginBody, request: Request) -> dict[str, object]:
    settings = _settings(request)
    ip = _client_ip(request)
    allowed = await request.app.state.limiter.allow(
        f"login:{ip or 'unknown'}",
        settings.login_rate_limit,
        settings.login_rate_window_seconds,
    )
    if not allowed:
        raise APIError(429, "rate_limited", "登录尝试过于频繁")
    async with get_sessionmaker()() as session:
        user = await session.scalar(select(User).where(User.email == body.email.strip().lower()))
        if user is None or not user.is_active or not verify_password(user.password_hash, body.password):
            await write_audit(
                settings=settings,
                actor_id=user.id if user else None,
                action="login_failed",
                request_id=getattr(request.state, "request_id", None),
                ip=ip,
                detail={"method": "password"},
            )
            raise APIError(401, "invalid_credentials", "邮箱或密码不正确")
        token, _jti, ttl = issue_token(user, settings)
        await write_audit(
            settings=settings,
            actor_id=user.id,
            action="login",
            request_id=getattr(request.state, "request_id", None),
            ip=ip,
            detail={"method": "password"},
        )
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_in": ttl,
            "user": {"id": str(user.id), "display_name": user.display_name, "role": user.role},
        }


@router.post("/auth/logout")
async def logout(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    settings = _settings(request)
    if credentials is not None and not settings.oidc_issuer:
        decoded = decode_unverified_for_logout(credentials.credentials)
        if decoded is not None:
            jti, exp = decoded
            await revoke_token(session, jti, exp)
    await write_audit(
        settings=settings,
        actor_id=actor.id,
        action="logout",
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"method": "local"},
    )
    return {"status": "ok"}


@router.get("/auth/me")
async def me(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    user = await _user(session, actor)
    return {"id": str(user.id), "display_name": user.display_name, "role": user.role}


@router.get("/documents")
async def list_documents(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    rows = await session.execute(
        text(
            f"""
            SELECT d.id::text, d.title, d.collection, d.visibility, d.source, d.status,
                   d.doc_key, d.created_at::text
            FROM documents d
            WHERE d.deleted_at IS NULL AND d.status = 'ready'
              AND d.source <> 'eval' AND {acl_sql()}
            ORDER BY d.created_at DESC
            """
        ),
        {
            "is_admin": actor.role == "admin",
            "uid": str(actor.id),
            "uid_text": str(actor.id),
            "role": actor.role,
        },
    )
    return {"documents": [dict(row) for row in rows.mappings()]}


@router.post("/documents")
async def upload_document(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    title: Annotated[str, Form()],
    collection: Annotated[str, Form()] = "knowledge",
    visibility: Annotated[str, Form()] = "lab",
    text: Annotated[str | None, Form()] = None,
    grant_emails: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> dict[str, str]:
    settings = _settings(request)
    user = await _user(session, actor)
    if user.role == "viewer":
        await write_audit(
            settings=settings,
            actor_id=user.id,
            action="permission_denied",
            resource_type="document",
            request_id=getattr(request.state, "request_id", None),
            detail={"operation": "upload"},
        )
        raise APIError(403, "forbidden", "查看者不能上传文档")
    if collection not in {"knowledge", "project", "meeting", "paper_note"}:
        raise APIError(400, "invalid_collection", "不支持的文档类型")
    if visibility not in {"lab", "restricted", "private"}:
        raise APIError(400, "invalid_visibility", "不支持的可见性")
    raw = text or ""
    if file is not None:
        payload = await file.read()
        if len(payload) > settings.max_upload_bytes:
            raise APIError(413, "file_too_large", "文件超过 5 MB")
        name = (file.filename or "").lower()
        if name.endswith(".pdf"):
            import io

            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(payload))
            raw = "\n".join(page.extract_text() or "" for page in reader.pages)
        else:
            raw = payload.decode("utf-8", errors="replace")
    raw = raw.strip()
    if not raw:
        raise APIError(400, "empty_document", "文档没有可用文本")
    grants: list[uuid.UUID] = []
    if grant_emails:
        for email in [part.strip().lower() for part in grant_emails.split(",") if part.strip()]:
            granted = await session.scalar(select(User).where(User.email == email))
            if granted is None:
                raise APIError(400, "unknown_grantee", "授权名单里有不存在的账号")
            grants.append(granted.id)
    try:
        document = await ingest_text(
            session,
            embedder=_embedder(request),
            owner_id=user.id,
            title=title.strip()[:300] or "未命名",
            text=raw,
            collection=collection,
            visibility=visibility,
            source="upload",
            grant_user_ids=grants,
        )
    except ValueError as exc:
        raise APIError(400, "empty_document", "文档没有可用文本") from exc
    await session.commit()
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="document_upload",
        resource_type="document",
        resource_id=str(document.id),
        request_id=getattr(request.state, "request_id", None),
        detail={
            "visibility": visibility,
            "collection": collection,
            "chars": len(raw),
            "grants": len(grants),
        },
    )
    return {"id": str(document.id), "status": document.status}


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    document = await session.get(Document, document_id)
    if document is None or document.deleted_at is not None:
        raise APIError(404, "not_found", "文档不存在")
    grants = list(
        await session.scalars(select(DocumentACL).where(DocumentACL.document_id == document.id))
    )
    if not can_read(Principal(id=actor.id, role=actor.role), document, grants):
        await write_audit(
            settings=_settings(request),
            actor_id=actor.id,
            action="permission_denied",
            resource_type="document",
            resource_id=str(document.id),
            request_id=getattr(request.state, "request_id", None),
            detail={"operation": "read"},
        )
        raise APIError(404, "not_found", "文档不存在")
    return {
        "id": str(document.id),
        "title": document.title,
        "collection": document.collection,
        "visibility": document.visibility,
        "doc_key": document.doc_key,
        "content": document.content_text,
    }


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    settings = _settings(request)
    user = await _user(session, actor)
    document = await session.get(Document, document_id)
    if document is None or document.deleted_at is not None:
        raise APIError(404, "not_found", "文档不存在")
    if user.role != "admin" and document.owner_id != user.id:
        await write_audit(
            settings=settings,
            actor_id=user.id,
            action="permission_denied",
            resource_type="document",
            resource_id=str(document.id),
            request_id=getattr(request.state, "request_id", None),
            detail={"operation": "delete"},
        )
        raise APIError(403, "forbidden", "只能删除自己上传的文档")
    from datetime import UTC, datetime

    document.deleted_at = datetime.now(UTC)
    document.status = "failed"
    await session.commit()
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="document_delete",
        resource_type="document",
        resource_id=str(document.id),
        request_id=getattr(request.state, "request_id", None),
        detail={"operation": "delete"},
    )
    return {"status": "deleted"}


@router.post("/search")
async def search(
    body: SearchBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    user = await _user(session, actor)
    hits = await search_knowledge_base(
        session,
        settings=_settings(request),
        embedder=_embedder(request),
        user=user,
        query=body.query,
        k=body.k,
        collection=body.collection,
        request_id=getattr(request.state, "request_id", None),
    )
    return {
        "hits": [
            {
                "chunk_id": hit["chunk_id"],
                "document_id": hit["document_id"],
                "doc_key": hit.get("doc_key"),
                "title": hit.get("title"),
                "heading": hit.get("heading"),
                "chunk_index": hit.get("chunk_index"),
                "excerpt": str(hit.get("content") or "")[:280],
                "score": round(float(hit.get("cosine") or 0.0), 4),
            }
            for hit in hits
        ]
    }


@router.post("/chat/stream")
async def chat_stream(
    body: ChatBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
) -> StreamingResponse:
    settings = _settings(request)
    allowed = await request.app.state.limiter.allow(
        f"chat:{actor.id}",
        settings.chat_rate_limit,
        settings.chat_rate_window_seconds,
    )
    if not allowed:
        raise APIError(429, "rate_limited", "提问过于频繁")
    request_id = getattr(request.state, "request_id", None)

    async def events() -> AsyncIterator[str]:
        import asyncio

        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

        async def emit(event: dict[str, object]) -> None:
            await queue.put(event)

        async def work() -> None:
            try:
                async with get_sessionmaker()() as session:
                    user = await session.get(User, actor.id)
                    if user is None:
                        await queue.put({"type": "error", "code": "unauthorized"})
                        return
                    conversation = None
                    if body.conversation_id is not None:
                        conversation = await session.get(Conversation, body.conversation_id)
                        if conversation is None or conversation.user_id != user.id:
                            await queue.put({"type": "error", "code": "not_found"})
                            return
                    if conversation is None:
                        conversation = Conversation(user_id=user.id, title=body.message[:40])
                        session.add(conversation)
                        await session.flush()
                    session.add(
                        Message(
                            conversation_id=conversation.id,
                            role="user",
                            content=body.message,
                            refused=False,
                            citations=[],
                        )
                    )
                    await session.commit()
                    result = await run_agent(
                        session=session,
                        user=user,
                        settings=settings,
                        embedder=_embedder(request),
                        llm=_llm(request),
                        question=body.message,
                        emit=emit,
                        request_id=request_id,
                    )
                    decision = result["decision"]
                    refused = bool(result["refused"])
                    llm_error = result.get("llm_error")
                    embedding_error = result.get("embedding_error")
                    done_error: str | None = None
                    if llm_error:
                        content = "模型服务暂时不可用，没有生成回答。请稍后重试。"
                        refused = False
                        saved_citations: list[dict[str, object]] = []
                        done_error = "llm_unavailable"
                    elif embedding_error:
                        content = "检索服务暂时不可用，没有生成回答。请稍后重试。"
                        refused = False
                        saved_citations = []
                        done_error = "embedding_unavailable"
                    elif refused and isinstance(decision, dict):
                        if decision.get("reason") == "model_declined":
                            content = "模型认为现有证据不足以回答这个问题。"
                        else:
                            content = (
                                "没有足够依据回答这个问题。"
                                f"决策分数 {float(decision.get('score') or 0):.2f}，"
                                f"阈值 {float(settings.evidence_score_min or 0):.2f}。"
                                "系统没有在证据不足时生成答案。"
                            )
                        saved_citations = []
                    else:
                        content = str(result["answer"])
                        saved_citations = (
                            list(result["citations"])
                            if isinstance(result["citations"], list)
                            else []
                        )
                    message = Message(
                        conversation_id=conversation.id,
                        role="assistant",
                        content=content,
                        refused=refused,
                        citations=saved_citations,
                    )
                    session.add(message)
                    await session.commit()
                    usage = result["usage"] if isinstance(result["usage"], dict) else {}
                    done_event: dict[str, object] = {
                        "type": "done",
                        "conversation_id": str(conversation.id),
                        "message_id": str(message.id),
                        "refused": refused,
                        "model": usage.get("model", settings.llm_model),
                        "usage": {
                            "prompt_tokens": usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0),
                        },
                    }
                    if done_error is not None:
                        done_event["error"] = done_error
                    await queue.put(done_event)
            except Exception:
                await queue.put({"type": "error", "code": "agent_failed"})
            finally:
                await queue.put(None)

        task = asyncio.create_task(work())
        while True:
            item = await queue.get()
            if item is None:
                break
            kind = str(item.get("type") or "message")
            yield f"event: {kind}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        await task

    return StreamingResponse(events(), media_type="text/event-stream")


@router.get("/conversations")
async def list_conversations(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    rows = await session.scalars(
        select(Conversation)
        .where(Conversation.user_id == actor.id)
        .order_by(Conversation.created_at.desc())
        .limit(30)
    )
    return {
        "conversations": [
            {"id": str(row.id), "title": row.title, "created_at": row.created_at.isoformat()}
            for row in rows
        ]
    }


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != actor.id:
        raise APIError(404, "not_found", "会话不存在")
    messages = await session.scalars(
        select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at)
    )
    return {
        "id": str(conversation.id),
        "title": conversation.title,
        "messages": [
            {
                "id": str(item.id),
                "role": item.role,
                "content": item.content,
                "refused": item.refused,
                "citations": item.citations,
            }
            for item in messages
        ],
    }


@router.get("/audit")
async def list_audit(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    action: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    if actor.role != "admin":
        await write_audit(
            settings=_settings(request),
            actor_id=actor.id,
            action="permission_denied",
            resource_type="audit",
            request_id=getattr(request.state, "request_id", None),
            detail={"operation": "list"},
        )
        raise APIError(403, "forbidden", "只有管理员可以查看审计")
    bounded = min(max(limit, 1), 200)
    sql = """
        SELECT id, created_at::text AS created_at, actor_id::text AS actor_id, action,
               resource_type, resource_id, request_id, detail
        FROM audit_events
    """
    params: dict[str, object] = {"lim": bounded}
    if action:
        sql += " WHERE action = :action"
        params["action"] = action
    sql += " ORDER BY id DESC LIMIT :lim"
    rows = await session.execute(text(sql), params)
    return {"events": [dict(row) for row in rows.mappings()]}


@router.post("/audit/retention")
async def retention(
    body: RetentionBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int]:
    settings = _settings(request)
    if actor.role != "admin":
        raise APIError(403, "forbidden", "只有管理员可以清理审计")
    days = body.days or settings.audit_retention_days
    if not 1 <= days <= 3650:
        raise APIError(400, "invalid_retention", "保留天数不在允许范围内")
    deleted = await run_retention(session, days=days)
    await write_audit(
        settings=settings,
        actor_id=actor.id,
        action="audit_retention",
        resource_type="audit",
        request_id=getattr(request.state, "request_id", None),
        detail={"days": days, "deleted": deleted},
    )
    return {"deleted": deleted}


def install_exception_handler(app: object) -> None:
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)

    @app.exception_handler(APIError)
    async def _handle(_request: Request, exc: APIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
