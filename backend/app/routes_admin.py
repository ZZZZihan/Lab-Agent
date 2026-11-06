"""Administrator API for user and group management (/api/v1/admin)."""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import write_audit
from app.auth import Actor, hash_password, validate_password
from app.db import get_session
from app.models import Group, GroupMember, User
from app.routes import APIError, _actor, _client_ip, _settings

router = APIRouter(prefix="/api/v1/admin")

ALLOWED_ROLES = frozenset({"admin", "member", "viewer"})
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


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


class CreateGroupBody(BaseModel):
    slug: str
    name: str


class SetGroupMembersBody(BaseModel):
    user_ids: list[uuid.UUID] = Field(default_factory=list)


def _serialize_user(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "email": user.email,
        "login_name": user.login_name,
        "display_name": user.display_name,
        "role": user.role,
        "is_active": bool(user.is_active),
        "must_change_password": user.must_change_password_by is not None,
        "must_change_password_by": (
            user.must_change_password_by.isoformat() if user.must_change_password_by else None
        ),
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


async def _serialize_group(session: AsyncSession, group: Group) -> dict[str, object]:
    member_rows = list(
        await session.scalars(
            select(User)
            .join(GroupMember, GroupMember.user_id == User.id)
            .where(GroupMember.group_id == group.id)
            .order_by(User.created_at.asc(), User.email.asc())
        )
    )
    return {
        "id": str(group.id),
        "slug": group.slug,
        "name": group.name,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "member_ids": [str(member.id) for member in member_rows],
        "members": [
            {
                "id": str(member.id),
                "email": member.email or member.login_name or str(member.id),
                "login_name": member.login_name,
                "display_name": member.display_name,
                "role": member.role,
            }
            for member in member_rows
        ],
    }


async def _require_admin(
    request: Request,
    actor: Actor,
    *,
    operation: str,
    resource_type: str = "user",
    resource_id: str | None = None,
) -> None:
    if actor.role != "admin":
        await write_audit(
            settings=_settings(request),
            actor_id=actor.id,
            action="permission_denied",
            resource_type=resource_type,
            resource_id=resource_id,
            request_id=getattr(request.state, "request_id", None),
            ip=_client_ip(request),
            detail={"operation": operation},
        )
        raise APIError(403, "forbidden", "只有管理员可以执行此操作")


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
    user.must_change_password_by = None
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


@router.get("/groups")
async def list_groups(
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(request, actor, operation="list_groups", resource_type="group")
    groups = list(await session.scalars(select(Group).order_by(Group.slug.asc())))
    return {
        "groups": [await _serialize_group(session, group) for group in groups],
    }


@router.post("/groups")
async def create_group(
    body: CreateGroupBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(request, actor, operation="create_group", resource_type="group")
    slug = body.slug.strip().lower()
    name = body.name.strip()
    if not slug or not SLUG_RE.match(slug):
        raise APIError(400, "invalid_slug", "组标识需为小写字母、数字、连字符或下划线")
    if not name or len(name) > 120:
        raise APIError(400, "invalid_name", "组名称不能为空且不超过 120 个字符")

    existing = await session.scalar(select(Group).where(Group.slug == slug))
    if existing is not None:
        raise APIError(409, "group_exists", "该组标识已存在")

    group = Group(slug=slug, name=name)
    session.add(group)
    await session.flush()
    serialized = await _serialize_group(session, group)
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="group_created",
        resource_type="group",
        resource_id=str(group.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"slug": slug},
    )
    return {**serialized, "group": serialized}


@router.put("/groups/{group_id}/members")
async def set_group_members(
    group_id: uuid.UUID,
    body: SetGroupMembersBody,
    request: Request,
    actor: Annotated[Actor, Depends(_actor)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, object]:
    await _require_admin(
        request,
        actor,
        operation="set_group_members",
        resource_type="group",
        resource_id=str(group_id),
    )
    group = await session.get(Group, group_id)
    if group is None:
        raise APIError(404, "not_found", "项目组不存在")

    desired_ids: set[uuid.UUID] = set()
    ordered_desired: list[uuid.UUID] = []
    for uid in body.user_ids:
        if uid not in desired_ids:
            desired_ids.add(uid)
            ordered_desired.append(uid)

    if desired_ids:
        found_users = list(await session.scalars(select(User.id).where(User.id.in_(desired_ids))))
        if len(found_users) != len(desired_ids):
            raise APIError(400, "unknown_user", "成员列表包含不存在的用户")

    current_rows = list(
        await session.scalars(
            select(GroupMember.user_id).where(GroupMember.group_id == group.id)
        )
    )
    current_ids = set(current_rows)

    added = len(desired_ids - current_ids)
    removed = len(current_ids - desired_ids)

    await session.execute(delete(GroupMember).where(GroupMember.group_id == group.id))
    for uid in ordered_desired:
        session.add(GroupMember(group_id=group.id, user_id=uid))
    await session.flush()
    serialized = await _serialize_group(session, group)
    await session.commit()

    await write_audit(
        settings=_settings(request),
        actor_id=actor.id,
        action="group_members_changed",
        resource_type="group",
        resource_id=str(group.id),
        request_id=getattr(request.state, "request_id", None),
        ip=_client_ip(request),
        detail={"added": added, "removed": removed},
    )
    return {**serialized, "group": serialized, "added": added, "removed": removed}
