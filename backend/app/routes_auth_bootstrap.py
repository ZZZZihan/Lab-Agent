"""First-admin bootstrap status and one-time initialization endpoints (/api/v1/auth)."""

from __future__ import annotations

import os
import secrets
import unicodedata
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import hash_password, issue_token, validate_password
from app.config import Settings
from app.db import get_session, get_sessionmaker
from app.importer.people import (
    RESERVED_LOGIN_NAMES,
    T1_CERT_RE,
    clean_email,
    normalize_person_name,
)
from app.importer.pii import is_pii_id_or_phone
from app.logging_conf import get_logger
from app.models import User
from app.routes import APIError, _client_ip, _serialize_auth_user, _settings

log = get_logger("auth.bootstrap")

router = APIRouter(prefix="/api/v1/auth")

BOOTSTRAP_LOCK_ID = 2026110901


class BootstrapAdminBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str
    email: str
    staff_no: str | None = None
    password: str
    setup_token: str | None = None


def _resolve_bootstrap_token(settings: Settings) -> str | None:
    return settings.resolve_bootstrap_token()


async def ensure_prod_bootstrap_token(
    session: AsyncSession,
    settings: Settings,
) -> Path | None:
    """Generate a 0600 one-time setup token file in prod when no admin exists yet."""
    if settings.oidc_issuer and settings.oidc_issuer.strip():
        return None
    has_any_admin = (
        await session.scalar(select(User.id).where(User.role == "admin").limit(1))
        is not None
    )
    if has_any_admin:
        return None
    if _resolve_bootstrap_token(settings) is not None:
        return None

    raw_path = (settings.bootstrap_token_file or "").strip() or "./data/bootstrap_admin.token"
    path = Path(raw_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{token}\n")
        os.chmod(path, 0o600)
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    log.info("bootstrap_admin_token_ready", token_file=str(path))
    return path


@router.get("/bootstrap-status")
async def bootstrap_status(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    settings = _settings(request)
    ip = _client_ip(request)
    allowed = await request.app.state.limiter.allow(
        f"bootstrap:status:ip:{ip or 'unknown'}",
        settings.login_ip_rate_limit,
        settings.login_ip_rate_window_seconds,
    )
    if not allowed:
        await write_audit(
            settings=settings,
            actor_id=None,
            action="login_rate_limited",
            request_id=getattr(request.state, "request_id", None),
            ip=ip,
            detail={"bucket": "ip"},
        )
        raise APIError(429, "rate_limited", "请求过于频繁，请稍后再试")

    oidc_mode = bool(settings.oidc_issuer and settings.oidc_issuer.strip())
    has_any_admin = (
        await session.scalar(select(User.id).where(User.role == "admin").limit(1))
        is not None
    )
    needs_bootstrap = (not oidc_mode) and (not has_any_admin)
    setup_token_required = bool(
        needs_bootstrap
        and (
            settings.app_env == "prod"
            or _resolve_bootstrap_token(settings) is not None
        )
    )
    return {
        "needs_bootstrap": needs_bootstrap,
        "setup_token_required": setup_token_required,
        "oidc_mode": oidc_mode,
    }


@router.post("/bootstrap-admin")
async def bootstrap_admin(
    body: BootstrapAdminBody,
    request: Request,
) -> dict[str, object]:
    settings = _settings(request)
    ip = _client_ip(request)
    allowed = await request.app.state.limiter.allow(
        f"bootstrap:admin:ip:{ip or 'unknown'}",
        settings.login_ip_rate_limit,
        settings.login_ip_rate_window_seconds,
    )
    if not allowed:
        await write_audit(
            settings=settings,
            actor_id=None,
            action="login_rate_limited",
            request_id=getattr(request.state, "request_id", None),
            ip=ip,
            detail={"bucket": "ip"},
        )
        raise APIError(429, "rate_limited", "请求过于频繁，请稍后再试")

    if bool(settings.oidc_issuer and settings.oidc_issuer.strip()):
        raise APIError(
            403,
            "bootstrap_disabled",
            "统一身份认证 (OIDC) 模式下不支持本地注册管理员",
        )

    clean_name = normalize_person_name(body.display_name)
    if not clean_name:
        raise APIError(422, "invalid_display_name", "请填写真实姓名")

    valid_email = clean_email(body.email)
    if valid_email is None:
        raise APIError(422, "invalid_email", "请输入有效的邮箱地址")

    staff_login: str | None = None
    if body.staff_no is not None and body.staff_no.strip():
        raw_staff = unicodedata.normalize("NFKC", body.staff_no).strip()
        if is_pii_id_or_phone(raw_staff):
            raise APIError(422, "staff_no_invalid", "工号不可填写身份证号或手机号")
        lowered_staff = raw_staff.lower()
        if lowered_staff in RESERVED_LOGIN_NAMES or not T1_CERT_RE.match(lowered_staff):
            raise APIError(
                422,
                "staff_no_invalid",
                "工号需为 6~12 位字母或数字（至少含 1 位数字），且不可为系统保留名",
            )
        staff_login = lowered_staff

    try:
        validate_password(body.password, valid_email)
    except ValueError as exc:
        raise APIError(422, "password_policy_violation", str(exc)) from exc

    async with get_sessionmaker()() as session:
        await session.execute(text("SELECT pg_advisory_xact_lock(2026110901)"))

        existing_admin = await session.scalar(
            select(User.id).where(User.role == "admin").limit(1)
        )
        if existing_admin is not None:
            raise APIError(
                409,
                "bootstrap_closed",
                "系统已存在管理员账号，初始化通道已关闭",
            )

        expected_token = _resolve_bootstrap_token(settings)
        if settings.app_env == "prod" or expected_token is not None:
            provided = (body.setup_token or "").strip()
            if (
                not expected_token
                or not provided
                or not secrets.compare_digest(provided, expected_token)
            ):
                raise APIError(
                    403,
                    "setup_token_invalid",
                    "管理员初始化令牌无效或缺失",
                )

        if (
            await session.scalar(
                select(User.id).where(func.lower(User.email) == valid_email).limit(1)
            )
            is not None
        ):
            raise APIError(409, "identity_taken", "该邮箱已被占用")

        if (
            staff_login is not None
            and await session.scalar(
                select(User.id)
                .where(func.lower(User.login_name) == staff_login)
                .limit(1)
            )
            is not None
        ):
            raise APIError(409, "identity_taken", "该工号已被占用")

        user = User(
            email=valid_email,
            login_name=staff_login,
            display_name=clean_name,
            password_hash=hash_password(body.password),
            role="admin",
            is_active=True,
            must_change_password_by=None,
        )
        session.add(user)
        await session.flush()

        token, _jti, ttl = issue_token(user, settings)
        serialized_user = _serialize_auth_user(user)
        await write_audit(
            settings=settings,
            actor_id=user.id,
            action="bootstrap_admin",
            resource_type="user",
            resource_id=str(user.id),
            request_id=getattr(request.state, "request_id", None),
            ip=ip,
            detail={"user_id": str(user.id)},
            session=session,
        )
        await session.commit()

        if settings.bootstrap_token_file and settings.bootstrap_token_file.strip():
            try:
                Path(settings.bootstrap_token_file.strip()).unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": ttl,
        "user": serialized_user,
    }
