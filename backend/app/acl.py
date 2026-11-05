"""Document ACL. The same rules are applied in Python and in retrieval SQL."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Document, DocumentACL, GroupMember


@dataclass(frozen=True)
class Principal:
    id: uuid.UUID
    role: str
    group_ids: frozenset[str] = frozenset()


async def load_user_group_ids(session: AsyncSession, user_id: uuid.UUID) -> frozenset[str]:
    rows = await session.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user_id))
    return frozenset(str(group_id) for group_id in rows)


def can_read(user: Principal, document: Document, grants: list[DocumentACL]) -> bool:
    if document.deleted_at is not None or document.status != "ready":
        return False
    if user.role == "admin" or document.owner_id == user.id:
        return True
    if document.visibility == "lab":
        return True
    if document.visibility == "private":
        return False
    if document.visibility != "restricted":
        return False
    for grant in grants:
        if grant.permission != "read":
            continue
        if grant.principal_type == "user" and grant.principal_id == str(user.id):
            return True
        if grant.principal_type == "role" and grant.principal_id == user.role:
            return True
        if grant.principal_type == "group" and grant.principal_id in user.group_ids:
            return True
    return False


def acl_sql() -> str:
    """SQL boolean expression. Bind :is_admin, :uid, :uid_text, :role."""
    return """
    (
      CAST(:is_admin AS boolean)
      OR d.owner_id = CAST(:uid AS uuid)
      OR d.visibility = 'lab'
      OR (
        d.visibility = 'restricted' AND EXISTS (
          SELECT 1 FROM document_acl a
          WHERE a.document_id = d.id
            AND a.permission = 'read'
            AND (
              (a.principal_type = 'user' AND a.principal_id = :uid_text)
              OR (a.principal_type = 'role' AND a.principal_id = :role)
              OR (
                a.principal_type = 'group'
                AND a.principal_id IN (
                  SELECT gm.group_id::text
                  FROM group_members gm
                  WHERE gm.user_id = CAST(:uid AS uuid)
                )
              )
            )
        )
      )
    )
    """
