"""Document ACL. The same rules are applied in Python and in retrieval SQL."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models import Document, DocumentACL


@dataclass(frozen=True)
class Principal:
    id: uuid.UUID
    role: str


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
            )
        )
      )
    )
    """
