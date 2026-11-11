"""HTTP API for login, documents, grounded chat, and the audit trail."""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hmac
import io
import ipaddress
import json
import re
import unicodedata
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.acl import Principal, acl_sql, can_read
from app.agent import run_agent
from app.audit import run_retention, scrub_audit_str, write_audit
from app.auth import (
    PAT_PREFIX,
    Actor,
    authenticate_local,
    authenticate_pat,
    decode_unverified_for_logout,
    ensure_utc,
    generate_pat,
    hash_password,
    issue_token,
    password_fingerprint,
    revoke_token,
    validate_password,
    verify_password_async,
)
from app.config import Settings
from app.db import get_session, get_sessionmaker
from app.importer.people import clean_email, name_to_pinyin, normalize_person_name
from app.importer.pii import escape_csv_cell
from app.llm import LLM
from app.logging_conf import get_logger
from app.maintenance import purge_document, scrub_missing_document_citations
from app.models import (
    ApiToken,
    Conversation,
    Document,
    DocumentACL,
    Group,
    Message,
    RevokedToken,
    User,
)
from app.rag.embedder import Embedder
from app.rag.extract import UploadRejected, detect_pasted_format, extract_upload
from app.rag.index_meta import check_index
from app.rag.ingest import _add_acl_grants, create_document
from app.tools import search_knowledge_base

router = APIRouter(prefix="/api/v1")
bearer = HTTPBearer(auto_error=False)
log = get_logger("routes")


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message


class LoginBody(BaseModel):
    identifier: str | None = None
    email: str | None = None
    password: str


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str
    email: str | None = None


class SearchBody(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=8, ge=1, le=20)
    collection: str | None = None


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None


class RetentionBody(BaseModel):
    days: int | None = None


class DocumentACLBody(BaseModel):
    visibility: str
    user_emails: list[str] = Field(default_factory=list)
    group_slugs: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


class CreateTokenBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    expires_in_days: int | None = Field(default=90, ge=1, le=3650)



async def _serialize_document_acl(
    session: AsyncSession,
    document: Document,
    grants: list[DocumentACL],
) -> dict[str, object]:
    user_ids: list[uuid.UUID] = []
    group_ids: list[uuid.UUID] = []
    roles: list[str] = []
    for grant in grants:
        if grant.permission != "read":
            continue
        if grant.principal_type == "user":
            try:
                user_ids.append(uuid.UUID(grant.principal_id))
            except ValueError:
                pass
        elif grant.principal_type == "group":
            try:
                group_ids.append(uuid.UUID(grant.principal_id))
            except ValueError:
                pass
        elif grant.principal_type == "role":
            roles.append(grant.principal_id)

    user_emails: list[str] = []
    if user_ids:
        user_rows = list(await session.scalars(select(User).where(User.id.in_(user_ids))))
        email_by_id = {u.id: u.email or u.login_name or str(u.id) for u in user_rows}
        user_emails = [email_by_id[uid] for uid in user_ids if uid in email_by_id]

    group_slugs: list[str] = []
    if group_ids:
        group_rows = list(await session.scalars(select(Group).where(Group.id.in_(group_ids))))
        slug_by_id = {g.id: g.slug for g in group_rows}
        group_slugs = [slug_by_id[gid] for gid in group_ids if gid in slug_by_id]

    return {
        "visibility": document.visibility,
        "user_emails": user_emails,
        "group_slugs": group_slugs,
        "roles": roles,
    }


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _embedder(request: Request) -> Embedder:
    return request.app.state.embedder  # type: ignore[no-any-return]


def _llm(request: Request) -> LLM:
    return request.app.state.llm  # type: ignore[no-any-return]


def _is_pat_allowed_request(request: Request) -> bool:
    """Return True if a Personal Access Token (`la_pat_`) is permitted for this route."""
    method = request.method.upper()
    path = request.url.path.rstrip("/")
    if method == "POST" and path in {"/api/v1/search", "/api/v1/chat", "/api/v1/chat/stream"}:
        return True
    if method == "GET" and (
        path == "/api/v1/documents"
        or (path.startswith("/api/v1/documents/") and not path.endswith("/acl"))
    ):
        return True
    if method == "GET" and (
        path == "/api/v1/conversations" or path.startswith("/api/v1/conversations/")
    ):
        return True
    return False


async def _actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Actor:
    if credentials is None:
        raise APIError(401, "unauthorized", "需要登录")
    settings = _settings(request)
    raw_token = credentials.credentials
    async with get_sessionmaker()() as session:
        if raw_token.startswith(PAT_PREFIX):
            try:
                actor = await authenticate_pat(session, raw_token)
            except PermissionError as exc:
                raise APIError(401, "unauthorized", "令牌无效或已过期") from exc
        else:
            try:
                actor = await authenticate_local(session, raw_token, settings)
            except PermissionError as exc:
                raise APIError(401, "unauthorized", "登录已失效") from exc

    if actor.auth_via_pat and not _is_pat_allowed_request(request):
        await write_audit(
            settings=settings,
            actor_id=actor.id,
            action="permission_denied",
            resource_type="api_token",
            resource_id=str(actor.token_id) if actor.token_id else None,
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={"operation": f"{request.method} {request.url.path}"},
        )
        raise APIError(403, "forbidden", "个人访问令牌无权访问此接口")

    if actor.must_change_password_by is not None:
        now_utc = datetime.now(UTC)
        exp = ensure_utc(actor.must_change_password_by)
        if now_utc > exp:
            raise APIError(
                403,
                "initial_password_expired",
                "初始口令已过期，请联系课题组管理员重置",
            )
        path = request.url.path.rstrip("/")
        if not path.endswith(("/auth/change-password", "/auth/me", "/auth/logout")):
            raise APIError(
                403,
                "password_change_required",
                "首次登录请先修改初始口令后再访问系统功能",
            )
    return actor


async def _user(session: AsyncSession, actor: Actor) -> User:
    user = await session.get(User, actor.id)
    if user is None or not user.is_active:
        raise APIError(401, "unauthorized", "登录已失效")
    return user


def _client_ip(request: Request) -> str | None:
    settings = _settings(request)
    configured_secret = (
        settings.internal_proxy_secret.get_secret_value()
        if settings.internal_proxy_secret is not None
        else ""
    )
    header_secret = request.headers.get("x-internal-proxy-secret") or ""
    if (
        configured_secret
        and header_secret
        and hmac.compare_digest(
            header_secret.encode("utf-8"),
            configured_secret.encode("utf-8"),
        )
    ):
        forwarded_ip = (request.headers.get("x-client-ip") or "").strip()
        if forwarded_ip:
            try:
                return str(ipaddress.ip_address(forwarded_ip))
            except ValueError:
                pass
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
        "index_ok": index_ok,
        "embedding_index_ok": index_ok,
        "embedding_index": settings.resolved_embedding_index,
        "embedding_base_host": settings.embedding_base_host,
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model,
        "llm_base_host": settings.llm_base_host,
        "retrieval_mode": settings.retrieval_mode,
        "calibrated": settings.calibrated,
        "calibration_source": (
            Path(settings.calibration_source).name if settings.calibration_source else None
        ),
    }


def _serialize_auth_user(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "display_name": user.display_name,
        "role": user.role,
        "email": user.email,
        "login_name": user.login_name,
        "must_change_password": user.must_change_password_by is not None,
        "must_change_password_by": (
            user.must_change_password_by.isoformat()
            if user.must_change_password_by is not None
            else None
        ),
    }


@router.post("/auth/login")
async def login(body: LoginBody, request: Request) -> dict[str, object]:
    settings = _settings(request)
    ip = _client_ip(request)
    raw = unicodedata.normalize("NFKC", (body.identifier or body.email or "")).strip()
    raw = re.sub(r"[​-‏﻿]", "", raw)
    if not raw:
        raise APIError(401, "invalid_credentials", "请输入登录名、姓名、学工号或邮箱")

    limited_bucket = await request.app.state.limiter.check_login(ip, raw.lower())
    if limited_bucket is not None:
        await write_audit(
            settings=settings,
            actor_id=None,
            action="login_rate_limited",
            request_id=getattr(request.state, "request_id", None),
            ip=ip,
            detail={"bucket": limited_bucket},
        )
        raise APIError(429, "rate_limited", "登录尝试过于频繁")

    async with get_sessionmaker()() as session:
        user: User | None = None
        if "@" in raw:
            user = await session.scalar(
                select(User).where(
                    User.email == raw.lower(),
                    User.is_active.is_(True),
                )
            )
        elif raw.isascii():
            user = await session.scalar(
                select(User).where(
                    User.login_name == raw.lower(),
                    User.is_active.is_(True),
                )
            )
        else:
            clean_name = normalize_person_name(raw)
            exact_matches = list(
                await session.scalars(
                    select(User).where(
                        User.display_name == clean_name,
                        User.is_active.is_(True),
                    )
                )
            )
            if len(exact_matches) > 1:
                await write_audit(
                    settings=settings,
                    actor_id=None,
                    action="login_failed",
                    request_id=getattr(request.state, "request_id", None),
                    ip=ip,
                    detail={"method": "password", "reason": "ambiguous_name"},
                )
                raise APIError(
                    401,
                    "ambiguous_name",
                    "存在重名账号，请使用登录名（如 zhangsan2）或学工号登录",
                )
            if len(exact_matches) == 1:
                user = exact_matches[0]
            else:
                base_pinyin = name_to_pinyin(clean_name)
                if base_pinyin:
                    candidates = list(
                        await session.scalars(
                            select(User).where(
                                User.login_name.like(f"{base_pinyin}%"),
                                User.is_active.is_(True),
                            )
                        )
                    )
                    family_re = re.compile(rf"^{re.escape(base_pinyin)}\d*$")
                    family = [
                        u
                        for u in candidates
                        if u.login_name and family_re.match(u.login_name)
                    ]
                    if len(family) > 1:
                        await write_audit(
                            settings=settings,
                            actor_id=None,
                            action="login_failed",
                            request_id=getattr(request.state, "request_id", None),
                            ip=ip,
                            detail={"method": "password", "reason": "ambiguous_name"},
                        )
                        raise APIError(
                            401,
                            "ambiguous_name",
                            "存在重名账号，请使用登录名（如 zhangsan2）或学工号登录",
                        )
                    if len(family) == 1:
                        user = family[0]

        password_ok = await verify_password_async(
            user.password_hash if user is not None and user.is_active else None,
            body.password,
        )
        if user is None or not user.is_active or not password_ok:
            await write_audit(
                settings=settings,
                actor_id=user.id if user else None,
                action="login_failed",
                request_id=getattr(request.state, "request_id", None),
                ip=ip,
                detail={"method": "password"},
            )
            raise APIError(401, "invalid_credentials", "邮箱、登录名或密码不正确")

        if user.must_change_password_by is not None and datetime.now(UTC) > ensure_utc(
            user.must_change_password_by
        ):
            await write_audit(
                settings=settings,
                actor_id=user.id,
                action="login_failed",
                request_id=getattr(request.state, "request_id", None),
                ip=ip,
                detail={"method": "password", "reason": "initial_password_expired"},
            )
            raise APIError(
                403,
                "initial_password_expired",
                "初始口令已过有效期，请联系管理员重置密码",
            )

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
            "user": _serialize_auth_user(user),
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
        session=session,
    )
    await session.commit()
    return {"status": "ok"}


@router.get("/auth/me")
async def me(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    user = await _user(session, actor)
    return _serialize_auth_user(user)


@router.post("/auth/change-password")
async def change_password(
    body: ChangePasswordBody,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    settings = _settings(request)
    user = await _user(session, actor)
    old_ok = await verify_password_async(user.password_hash, body.old_password)
    if not old_ok:
        raise APIError(401, "invalid_credentials", "当前口令不正确")
    if body.new_password == body.old_password:
        raise APIError(400, "invalid_password", "新口令不能与旧口令相同")

    email_bound = False
    target_email = user.email
    if body.email is not None and body.email.strip():
        cleaned = clean_email(body.email)
        if cleaned is None or len(cleaned) > 320:
            raise APIError(400, "invalid_email", "邮箱格式不正确")
        existing = await session.scalar(
            select(User).where(User.email == cleaned, User.id != user.id)
        )
        if existing is not None:
            raise APIError(409, "email_taken", "该邮箱已被其他账号使用")
        target_email = cleaned
        email_bound = True

    try:
        validate_password(body.new_password, target_email, settings=settings)
    except ValueError as exc:
        raise APIError(400, "invalid_password", str(exc)) from exc

    if email_bound and target_email is not None:
        user.email = target_email
    user.password_hash = await asyncio.to_thread(hash_password, body.new_password)
    user.must_change_password_by = None
    if credentials is not None and not settings.oidc_issuer:
        decoded = decode_unverified_for_logout(credentials.credentials)
        if decoded is not None:
            cur_jti, cur_exp = decoded
            allow_key = f"allow:{cur_jti[:24]}:{password_fingerprint(user.password_hash)}"
            if await session.get(RevokedToken, allow_key) is None:
                session.add(
                    RevokedToken(
                        jti=allow_key,
                        expires_at=datetime.fromtimestamp(cur_exp, UTC),
                    )
                )
    await session.flush()
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="user_change_self_password",
        resource_type="user",
        resource_id=str(user.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"email_bound": email_bound},
        session=session,
    )
    await session.commit()
    await session.refresh(user)
    new_token, _new_jti, ttl = issue_token(user, settings)
    return {
        "status": "ok",
        "access_token": new_token,
        "token_type": "bearer",
        "expires_in": ttl,
        "user": _serialize_auth_user(user),
    }


def _serialize_api_token(tok: ApiToken) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": str(tok.id),
        "name": tok.name,
        "prefix": tok.prefix,
        "created_at": tok.created_at.isoformat() if tok.created_at else None,
        "last_used_at": tok.last_used_at.isoformat() if tok.last_used_at else None,
        "expires_at": tok.expires_at.isoformat() if tok.expires_at else None,
        "revoked_at": tok.revoked_at.isoformat() if tok.revoked_at else None,
        "is_expired": bool(tok.expires_at is not None and tok.expires_at <= now),
    }


@router.get("/tokens")
async def list_tokens(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _user(session, actor)
    rows = list(
        await session.scalars(
            select(ApiToken)
            .where(ApiToken.user_id == actor.id)
            .order_by(ApiToken.created_at.desc())
        )
    )
    return {"tokens": [_serialize_api_token(tok) for tok in rows]}


@router.post("/tokens", status_code=201)
async def create_token(
    body: CreateTokenBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    settings = _settings(request)
    user = await _user(session, actor)
    clean_name = body.name.strip()
    if not clean_name:
        raise APIError(400, "invalid_name", "令牌名称不能为空")
    raw_token, token_hash, prefix = generate_pat()
    now = datetime.now(UTC)
    expires_at = (
        now + timedelta(days=body.expires_in_days)
        if body.expires_in_days is not None
        else None
    )
    tok = ApiToken(
        user_id=user.id,
        name=clean_name,
        token_hash=token_hash,
        prefix=prefix,
        created_at=now,
        expires_at=expires_at,
    )
    session.add(tok)
    await session.flush()
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="token_create",
        resource_type="api_token",
        resource_id=str(tok.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "name": scrub_audit_str(clean_name, max_len=100),
            "prefix": prefix,
            "expires_in_days": body.expires_in_days,
        },
        session=session,
    )
    await session.commit()
    await session.refresh(tok)
    payload = _serialize_api_token(tok)
    payload["token"] = raw_token
    return payload


@router.delete("/tokens/{token_id}")
async def revoke_personal_token(
    token_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    settings = _settings(request)
    user = await _user(session, actor)
    tok = await session.scalar(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user.id)
    )
    if tok is None:
        raise APIError(404, "not_found", "令牌不存在")
    if tok.revoked_at is None:
        tok.revoked_at = datetime.now(UTC)
    await session.flush()
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="token_revoke",
        resource_type="api_token",
        resource_id=str(tok.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "name": scrub_audit_str(tok.name, max_len=100),
            "prefix": tok.prefix,
        },
        session=session,
    )
    await session.commit()
    return {"status": "revoked", "id": str(tok.id)}



@router.get("/groups")
async def list_public_groups(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _user(session, actor)
    rows = list(await session.scalars(select(Group).order_by(Group.slug.asc())))
    return {
        "groups": [
            {"slug": group.slug, "name": group.name}
            for group in rows
        ]
    }


@router.get("/documents")
async def list_documents(
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    rows = await session.execute(
        text(
            f"""
            SELECT d.id::text AS id,
                   d.title AS title,
                   d.collection AS collection,
                   d.visibility AS visibility,
                   d.source AS source,
                   d.status AS status,
                   CASE
                     WHEN CAST(:is_admin AS boolean) OR d.owner_id = CAST(:uid AS uuid)
                     THEN d.error_code
                     ELSE NULL
                   END AS error_code,
                   d.source_format AS source_format,
                   d.original_filename AS original_filename,
                   d.doc_key AS doc_key,
                   d.created_at::text AS created_at
            FROM documents d
            WHERE d.deleted_at IS NULL
              AND d.source <> 'eval'
              AND (
                d.status = 'ready'
                OR d.owner_id = CAST(:uid AS uuid)
                OR CAST(:is_admin AS boolean)
              )
              AND {acl_sql()}
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


@router.post("/documents", status_code=202)
async def upload_document(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    title: Annotated[str, Form()],
    collection: Annotated[str, Form()] = "knowledge",
    visibility: Annotated[str, Form()] = "lab",
    text: Annotated[str | None, Form()] = None,
    grant_emails: Annotated[str | None, Form()] = None,
    grant_groups: Annotated[str | None, Form()] = None,
    grant_roles: Annotated[str | None, Form()] = None,
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
    allowed = await request.app.state.limiter.allow(
        f"upload:{user.id}",
        settings.upload_rate_limit,
        settings.upload_rate_window_seconds,
    )
    if not allowed:
        raise APIError(429, "rate_limited", "上传过于频繁")
    if collection not in {"knowledge", "project", "meeting", "paper_note"}:
        raise APIError(400, "invalid_collection", "不支持的文档类型")
    if visibility not in {"lab", "restricted", "private"}:
        raise APIError(400, "invalid_visibility", "不支持的可见性")

    has_file = file is not None and bool((file.filename or "").strip())
    has_text = text is not None and bool(text.strip())
    if has_file and has_text:
        raise APIError(400, "ambiguous_input", "不能同时提供文件和粘贴文本")
    if not has_file and not has_text:
        raise APIError(400, "empty_document", "文档没有可用文本")

    original_filename: str | None = None
    if has_file and file is not None:
        payload = await file.read(settings.max_upload_bytes + 1)
        if len(payload) > settings.max_upload_bytes:
            raise APIError(413, "file_too_large", "文件超过 5 MB")
        original_filename = (file.filename or "").strip()[:255] or None
        try:
            extracted = await asyncio.to_thread(
                extract_upload,
                original_filename or "",
                payload,
                settings.max_document_chars,
            )
        except UploadRejected as exc:
            raise APIError(exc.status, exc.code, exc.message) from exc
        raw = extracted.text
        source_format = extracted.source_format
        byte_len = len(payload)
    else:
        raw_input = (text or "").strip()
        byte_payload = raw_input.encode("utf-8")
        byte_len = len(byte_payload)
        source_format = detect_pasted_format(raw_input)
        pseudo_name = "pasted.md" if source_format == "markdown" else "pasted.txt"
        try:
            extracted = await asyncio.to_thread(
                extract_upload,
                pseudo_name,
                byte_payload,
                settings.max_document_chars,
            )
        except UploadRejected as exc:
            raise APIError(exc.status, exc.code, exc.message) from exc
        raw = extracted.text

    raw = raw.strip()
    if not raw:
        raise APIError(400, "empty_document", "文档没有可用文本")
    grants: list[uuid.UUID] = []
    if grant_emails:
        for email in [part.strip().lower() for part in grant_emails.split(",") if part.strip()]:
            granted = await session.scalar(
                select(User).where(User.email == email if "@" in email else User.login_name == email)
            )
            if granted is None:
                raise APIError(400, "unknown_grantee", "授权名单里有不存在的账号")
            if granted.id not in grants:
                grants.append(granted.id)
    group_grants: list[uuid.UUID] = []
    if grant_groups:
        for slug in [part.strip().lower() for part in grant_groups.split(",") if part.strip()]:
            group = await session.scalar(select(Group).where(Group.slug == slug))
            if group is None:
                raise APIError(400, "unknown_group", "授权名单里有不存在的项目组")
            if group.id not in group_grants:
                group_grants.append(group.id)
    role_grants: list[str] = []
    if grant_roles:
        for role_name in [part.strip() for part in grant_roles.split(",") if part.strip()]:
            if role_name not in {"admin", "member", "viewer"}:
                raise APIError(400, "invalid_role", "授权角色必须是 admin、member 或 viewer")
            if role_name not in role_grants:
                role_grants.append(role_name)
    try:
        document = await create_document(
            session,
            owner_id=user.id,
            title=title.strip()[:300] or "未命名",
            text=raw,
            source_format=source_format,
            collection=collection,
            visibility=visibility,
            source="upload",
            grant_user_ids=grants,
            grant_group_ids=group_grants,
            grant_roles=role_grants,
            original_filename=original_filename,
        )
    except ValueError as exc:
        raise APIError(400, "empty_document", "文档没有可用文本") from exc
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="document_upload",
        resource_type="document",
        resource_id=str(document.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "visibility": visibility,
            "collection": collection,
            "format": source_format,
            "bytes": byte_len,
            "chars": len(raw),
            "grants": len(grants) + len(group_grants) + len(role_grants),
        },
        session=session,
    )
    await session.commit()
    return {"id": str(document.id), "status": document.status}


@router.get("/documents/{document_id}")
async def get_document(
    document_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    document = await session.get(Document, document_id)
    if document is None or document.deleted_at is not None or document.source == "eval":
        raise APIError(404, "not_found", "文档不存在")
    is_owner_or_admin = actor.role == "admin" or document.owner_id == actor.id
    if not is_owner_or_admin and document.status != "ready":
        raise APIError(404, "not_found", "文档不存在")
    grants = list(
        await session.scalars(select(DocumentACL).where(DocumentACL.document_id == document.id))
    )
    if not is_owner_or_admin and not can_read(
        Principal(id=actor.id, role=actor.role, group_ids=actor.group_ids),
        document,
        grants,
    ):
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
    response_payload: dict[str, object] = {
        "id": str(document.id),
        "title": document.title,
        "collection": document.collection,
        "visibility": document.visibility,
        "doc_key": document.doc_key,
        "content": document.content_text,
    }
    if actor.role == "admin" or document.owner_id == actor.id:
        response_payload["acl"] = await _serialize_document_acl(session, document, grants)
    return response_payload


@router.put("/documents/{document_id}/acl")
async def update_document_acl(
    document_id: uuid.UUID,
    body: DocumentACLBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    settings = _settings(request)
    user = await _user(session, actor)
    document = await session.scalar(
        select(Document).where(Document.id == document_id).with_for_update()
    )
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
            ip=_client_ip(request),
            detail={"operation": "acl_edit"},
        )
        raise APIError(404, "not_found", "文档不存在")

    visibility = body.visibility.strip()
    if visibility not in {"lab", "restricted", "private"}:
        raise APIError(400, "invalid_visibility", "不支持的可见性")

    user_ids: list[uuid.UUID] = []
    for raw_email in body.user_emails:
        email = raw_email.strip().lower()
        if not email:
            continue
        granted = await session.scalar(
            select(User).where(User.email == email if "@" in email else User.login_name == email)
        )
        if granted is None:
            raise APIError(400, "unknown_grantee", "授权名单里有不存在的账号")
        if granted.id not in user_ids:
            user_ids.append(granted.id)

    group_ids: list[uuid.UUID] = []
    for raw_slug in body.group_slugs:
        slug = raw_slug.strip().lower()
        if not slug:
            continue
        group = await session.scalar(select(Group).where(Group.slug == slug))
        if group is None:
            raise APIError(400, "unknown_group", "授权名单里有不存在的项目组")
        if group.id not in group_ids:
            group_ids.append(group.id)

    role_names: list[str] = []
    for raw_role in body.roles:
        role_name = raw_role.strip()
        if not role_name:
            continue
        if role_name not in {"admin", "member", "viewer"}:
            raise APIError(400, "invalid_role", "授权角色必须是 admin、member 或 viewer")
        if role_name not in role_names:
            role_names.append(role_name)

    document.visibility = visibility
    await session.execute(delete(DocumentACL).where(DocumentACL.document_id == document.id))
    _add_acl_grants(
        session,
        document.id,
        grant_user_ids=user_ids,
        grant_group_ids=group_ids,
        grant_roles=role_names,
    )
    await session.flush()
    new_grants = list(
        await session.scalars(select(DocumentACL).where(DocumentACL.document_id == document.id))
    )
    acl_payload = await _serialize_document_acl(session, document, new_grants)
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="acl_changed",
        resource_type="document",
        resource_id=str(document.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={
            "visibility": visibility,
            "users": len(user_ids),
            "groups": len(group_ids),
            "roles": len(role_names),
        },
        session=session,
    )
    await session.commit()
    return {
        "id": str(document.id),
        "visibility": document.visibility,
        "acl": acl_payload,
    }


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: uuid.UUID,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    purge: bool = False,
) -> dict[str, object]:
    settings = _settings(request)
    user = await _user(session, actor)
    document = await session.get(Document, document_id)
    if document is None or (document.deleted_at is not None and not purge):
        raise APIError(404, "not_found", "文档不存在")
    if user.role != "admin" and document.owner_id != user.id:
        await write_audit(
            settings=settings,
            actor_id=user.id,
            action="permission_denied",
            resource_type="document",
            resource_id=str(document.id),
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={"operation": "purge" if purge else "delete"},
        )
        if document.deleted_at is not None:
            raise APIError(404, "not_found", "文档不存在")
        raise APIError(403, "forbidden", "只能删除自己上传的文档")
    if purge:
        doc_id_str = str(document.id)
        messages_scrubbed = await purge_document(session, document.id)
        await write_audit(
            settings=settings,
            actor_id=user.id,
            action="document_purged",
            resource_type="document",
            resource_id=doc_id_str,
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={"messages_scrubbed": messages_scrubbed},
            session=session,
        )
        await session.commit()
        return {"status": "purged", "messages_scrubbed": messages_scrubbed}

    from datetime import UTC, datetime

    document.deleted_at = datetime.now(UTC)
    await session.flush()
    await write_audit(
        settings=settings,
        actor_id=user.id,
        action="document_delete",
        resource_type="document",
        resource_id=str(document.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"operation": "delete"},
        session=session,
    )
    await session.commit()
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
        group_ids=actor.group_ids,
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


async def _noop_chat_emit(_event: dict[str, object]) -> None:
    return None


@router.post("/chat")
async def chat_json(
    body: ChatBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    settings = _settings(request)
    user = await _user(session, actor)
    allowed = await request.app.state.limiter.allow(
        f"chat:{actor.id}",
        settings.chat_rate_limit,
        settings.chat_rate_window_seconds,
    )
    if not allowed:
        raise APIError(429, "rate_limited", "提问过于频繁")
    request_id = getattr(request.state, "request_id", None)
    conversation = None
    if body.conversation_id is not None:
        conversation = await session.get(Conversation, body.conversation_id)
        if conversation is None or conversation.user_id != user.id:
            raise APIError(404, "not_found", "会话不存在")
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
        emit=_noop_chat_emit,
        request_id=request_id,
        group_ids=actor.group_ids,
    )
    decision = result["decision"]
    refused = bool(result["refused"])
    llm_error = result.get("llm_error")
    embedding_error = result.get("embedding_error")
    reason: str | None = (
        str(decision.get("reason"))
        if refused and isinstance(decision, dict) and decision.get("reason") is not None
        else None
    )
    if llm_error:
        content = "模型服务暂时不可用，没有生成回答。请稍后重试。"
        refused = True
        reason = str(llm_error)
        saved_citations: list[dict[str, object]] = []
    elif embedding_error:
        content = "检索服务暂时不可用，没有生成回答。请稍后重试。"
        refused = True
        reason = str(embedding_error)
        saved_citations = []
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
        raw_citations = (
            list(result["citations"]) if isinstance(result["citations"], list) else []
        )
        saved_citations = await scrub_missing_document_citations(session, raw_citations)
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
    return {
        "conversation_id": str(conversation.id),
        "message_id": str(message.id),
        "answer": content,
        "content": content,
        "refused": refused,
        "reason": reason,
        "citations": saved_citations,
        "model": usage.get("model", settings.llm_model),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        },
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
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

        async def emit(event: dict[str, object]) -> None:
            await queue.put(event)

        async def work() -> None:
            try:
                async with get_sessionmaker()() as session:
                    user = await session.get(User, actor.id)
                    if user is None or not user.is_active:
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
                        group_ids=actor.group_ids,
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
                        raw_citations = (
                            list(result["citations"])
                            if isinstance(result["citations"], list)
                            else []
                        )
                        saved_citations = await scrub_missing_document_citations(
                            session, raw_citations
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
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("chat_stream_failed", request_id=request_id)
                await queue.put({"type": "error", "code": "agent_failed"})
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(work())
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind = str(item.get("type") or "message")
                yield f"event: {kind}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
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


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != actor.id:
        raise APIError(404, "not_found", "会话不存在")
    await session.delete(conversation)
    await session.commit()
    return {"status": "deleted"}

def _escape_csv_cell(value: object) -> str:
    return escape_csv_cell(value)


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    elif "T" in raw and " " in raw.split("T", 1)[1]:
        date_part, time_part = raw.split("T", 1)
        raw = f"{date_part}T{time_part.replace(' ', '+', 1)}"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise APIError(400, "invalid_timestamp", "时间格式无效") from exc
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _build_audit_filters(
    *,
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    resource_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    before_id: int | None = None,
) -> tuple[list[str], dict[str, object]]:
    conditions: list[str] = []
    params: dict[str, object] = {}
    if action:
        conditions.append("a.action = :action")
        params["action"] = action
    if actor_id is not None:
        conditions.append("a.actor_id = CAST(:actor_id AS uuid)")
        params["actor_id"] = str(actor_id)
    if resource_id:
        conditions.append("a.resource_id = :resource_id")
        params["resource_id"] = resource_id
    since_dt = _parse_iso_timestamp(since)
    if since_dt is not None:
        conditions.append("a.created_at >= :since")
        params["since"] = since_dt
    until_dt = _parse_iso_timestamp(until)
    if until_dt is not None:
        conditions.append("a.created_at <= :until")
        params["until"] = until_dt
    if before_id is not None:
        conditions.append("a.id < :before_id")
        params["before_id"] = before_id
    return conditions, params


@router.get("/audit")
async def list_audit(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    resource_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    before_id: int | None = None,
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
    conditions, params = _build_audit_filters(
        action=action,
        actor_id=actor_id,
        resource_id=resource_id,
        since=since,
        until=until,
        before_id=before_id,
    )
    params["lim"] = bounded + 1
    where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT a.id AS id,
               a.created_at::text AS created_at,
               a.actor_id::text AS actor_id,
               u.display_name AS actor_name,
               a.action AS action,
               a.resource_type AS resource_type,
               a.resource_id AS resource_id,
               a.request_id AS request_id,
               a.detail AS detail
        FROM audit_events a
        LEFT JOIN users u ON u.id = a.actor_id
        {where_sql}
        ORDER BY a.id DESC
        LIMIT :lim
    """
    rows = [dict(row) for row in (await session.execute(text(sql), params)).mappings()]
    has_more = len(rows) > bounded
    events = rows[:bounded]
    next_before_id = int(events[-1]["id"]) if has_more and events else None
    return {"events": events, "next_before_id": next_before_id}


@router.get("/audit/export.csv")
async def export_audit_csv(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    resource_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> Response:
    settings = _settings(request)
    if actor.role != "admin":
        await write_audit(
            settings=settings,
            actor_id=actor.id,
            action="permission_denied",
            resource_type="audit",
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={"operation": "export"},
        )
        raise APIError(403, "forbidden", "只有管理员可以导出审计")
    conditions, params = _build_audit_filters(
        action=action,
        actor_id=actor_id,
        resource_id=resource_id,
        since=since,
        until=until,
    )
    params["lim"] = 50000
    where_sql = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT a.id AS id,
               a.created_at::text AS created_at,
               a.actor_id::text AS actor_id,
               u.display_name AS actor_name,
               a.action AS action,
               a.resource_type AS resource_type,
               a.resource_id AS resource_id,
               a.request_id AS request_id,
               a.ip_hash AS ip_hash,
               a.detail AS detail,
               a.prev_hash AS prev_hash,
               a.row_hash AS row_hash
        FROM audit_events a
        LEFT JOIN users u ON u.id = a.actor_id
        {where_sql}
        ORDER BY a.id DESC
        LIMIT :lim
    """
    rows = [dict(row) for row in (await session.execute(text(sql), params)).mappings()]
    await write_audit(
        settings=settings,
        actor_id=actor.id,
        action="audit_export",
        resource_type="audit",
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"rows": len(rows)},
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "id",
            "created_at",
            "actor_id",
            "actor_name",
            "action",
            "resource_type",
            "resource_id",
            "request_id",
            "ip_hash",
            "detail",
            "prev_hash",
            "row_hash",
        ]
    )
    for row in rows:
        detail_json = json.dumps(
            row.get("detail") or {}, ensure_ascii=False, sort_keys=True
        )
        writer.writerow(
            [
                row.get("id") or "",
                _escape_csv_cell(row.get("created_at")),
                _escape_csv_cell(row.get("actor_id")),
                _escape_csv_cell(row.get("actor_name")),
                _escape_csv_cell(row.get("action")),
                _escape_csv_cell(row.get("resource_type")),
                _escape_csv_cell(row.get("resource_id")),
                _escape_csv_cell(row.get("request_id")),
                _escape_csv_cell(row.get("ip_hash")),
                _escape_csv_cell(detail_json),
                _escape_csv_cell((str(row.get("prev_hash") or "")).strip()),
                _escape_csv_cell((str(row.get("row_hash") or "")).strip()),
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="audit_export.csv"'},
    )


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
        ip=_client_ip(request),
        detail={"days": days, "deleted": deleted},
        session=session,
    )
    await session.commit()
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
