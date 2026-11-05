"""Administrator API for user management (/api/v1/admin)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import Actor, hash_password, validate_password
from app.db import get_session
from app.models import User
from app.routes import APIError, _actor, _client_ip, _settings

router = APIRouter(prefix="/api/v1/admin")

ALLOWED_ROLES = frozenset({"admin", "member", "viewer"})


class CreateUserBody(BaseModel):
    email: str
    display_name: str
    role: str
    password: str


class UpdateUserBody(BaseModel):
    display_name: str | None = None
    role: str | None = None
    is_active: bool | None = None


class ResetPasswordBody(BaseModel):
    password: str


def _serialize_user(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": bool(user.is_active),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


async def _require_admin(
    request: Request,
    actor: Actor,
    *,
    operation: str,
    resource_id: str | None = None,
) -> None:
    if actor.role != "admin":
        await write_audit(
            settings=_settings(request),
            actor_id=actor.id,
            action="permission_denied",
            resource_type="user",
            resource_id=resource_id,
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={"operation": operation},
        )
        raise APIError(403, "forbidden", "只有管理员可以管理用户")


@router.get("/users")
async def list_users(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(request, actor, operation="list_users")
    rows = list(await session.scalars(select(User).order_by(User.created_at.asc(), User.email.asc())))
    return {"users": [_serialize_user(user) for user in rows]}


@router.post("/users")
async def create_user(
    body: CreateUserBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(request, actor, operation="create_user")
    email = body.email.strip().lower()
    display_name = body.display_name.strip()
    role = body.role.strip()
    if not email or "@" not in email:
        raise APIError(400, "invalid_email", "请输入有效的邮箱地址")
    if not display_name:
        raise APIError(400, "invalid_display_name", "姓名不能为空")
    if role not in ALLOWED_ROLES:
        raise APIError(400, "invalid_role", "角色必须是 admin、member 或 viewer")
    try:
        validate_password(body.password, email)
    except ValueError as exc:
        raise APIError(400, "weak_password", str(exc)) from exc

    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise APIError(409, "email_exists", "该邮箱已存在")

    user = User(
        email=email,
        display_name=display_name,
        role=role,
        password_hash=hash_password(body.password),
        is_active=True,
    )
    session.add(user)
    await session.flush()
    serialized = _serialize_user(user)
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="user_created",
        resource_type="user",
        resource_id=str(user.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"role": role},
    )
    return {**serialized, "user": serialized}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    body: UpdateUserBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(request, actor, operation="update_user", resource_id=str(user_id))
    user = await session.get(User, user_id)
    if user is None:
        raise APIError(404, "not_found", "用户不存在")

    if user.id == actor.id:
        if body.is_active is False:
            raise APIError(400, "cannot_modify_self", "管理员不能降级或停用自己")
        if body.role is not None and body.role.strip() != "admin":
            raise APIError(400, "cannot_modify_self", "管理员不能降级或停用自己")

    changed_fields: list[str] = []
    detail: dict[str, object] = {}

    if body.display_name is not None:
        new_name = body.display_name.strip()
        if not new_name:
            raise APIError(400, "invalid_display_name", "姓名不能为空")
        if new_name != user.display_name:
            user.display_name = new_name
            changed_fields.append("display_name")

    if body.role is not None:
        new_role = body.role.strip()
        if new_role not in ALLOWED_ROLES:
            raise APIError(400, "invalid_role", "角色必须是 admin、member 或 viewer")
        if new_role != user.role:
            detail["role_from"] = user.role
            detail["role_to"] = new_role
            user.role = new_role
            changed_fields.append("role")

    if body.is_active is not None and body.is_active != user.is_active:
        user.is_active = body.is_active
        changed_fields.append("is_active")

    detail["fields"] = changed_fields
    await session.flush()
    serialized = _serialize_user(user)
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="user_updated",
        resource_type="user",
        resource_id=str(user.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail=detail,
    )
    return {**serialized, "user": serialized}


@router.post("/users/{user_id}/password")
async def reset_user_password(
    user_id: uuid.UUID,
    body: ResetPasswordBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="reset_password",
        resource_id=str(user_id),
    )
    user = await session.get(User, user_id)
    if user is None:
        raise APIError(404, "not_found", "用户不存在")

    try:
        validate_password(body.password, user.email)
    except ValueError as exc:
        raise APIError(400, "weak_password", str(exc)) from exc

    user.password_hash = hash_password(body.password)
    await session.flush()
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="user_password_reset",
        resource_type="user",
        resource_id=str(user.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={},
    )
    return {"status": "ok", "id": str(user.id)}
