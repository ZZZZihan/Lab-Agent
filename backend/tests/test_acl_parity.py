"""Randomized SQL vs Python ACL parity test across 200 combinations on real Postgres."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime

from app.acl import Principal, acl_sql, can_read
from app.auth import UNUSABLE_PASSWORD_HASH
from app.db import get_sessionmaker
from app.models import Document, DocumentACL, Group, GroupMember, User
from sqlalchemy import text

ROLES = ("admin", "member", "viewer")
VISIBILITIES = ("lab", "restricted", "private")
STATUSES = ("ready", "ready", "ready", "pending", "failed")


async def test_acl_sql_and_can_read_parity_200_combinations() -> None:
    rng = random.Random(20261105)
    maker = get_sessionmaker()

    async with maker() as session:
        # Create a pool of groups
        groups: list[Group] = []
        for idx in range(6):
            group = Group(
                id=uuid.uuid4(),
                slug=f"parity-group-{idx}",
                name=f"Parity Group {idx}",
            )
            session.add(group)
            groups.append(group)

        # Create a pool of users with random roles and group memberships
        users: list[User] = []
        user_groups: dict[uuid.UUID, set[str]] = {}
        for idx in range(15):
            role = ROLES[idx % len(ROLES)] if idx < 6 else rng.choice(ROLES)
            user = User(
                id=uuid.uuid4(),
                email=f"parity-user-{idx}@qingheng.lab",
                display_name=f"Parity User {idx}",
                role=role,
                password_hash=UNUSABLE_PASSWORD_HASH,
                is_active=True,
            )
            session.add(user)
            users.append(user)

        await session.flush()

        for user in users:
            chosen_groups = rng.sample(groups, k=rng.randint(0, 3))
            user_groups[user.id] = {str(g.id) for g in chosen_groups}
            for grp in chosen_groups:
                session.add(GroupMember(group_id=grp.id, user_id=user.id))

        await session.flush()

        # Generate 200 randomized (user, role, group_members, document_visibility, grants) combinations
        combinations: list[tuple[User, Document, list[DocumentACL]]] = []
        pending_grants: list[tuple[Document, list[DocumentACL]]] = []
        for idx in range(200):
            actor = rng.choice(users)
            owner = actor if rng.random() < 0.2 else rng.choice(users)
            visibility = rng.choice(VISIBILITIES)
            status = rng.choice(STATUSES)
            deleted_at = datetime.now(UTC) if rng.random() < 0.12 else None

            doc = Document(
                id=uuid.uuid4(),
                doc_key=f"parity-doc-{idx}",
                source="upload",
                source_format="markdown",
                title=f"Parity Doc {idx}",
                collection="project",
                visibility=visibility,
                status=status,
                owner_id=owner.id,
                content_text=f"## 章节\n测试块 {idx}",
                deleted_at=deleted_at,
            )
            session.add(doc)

            grants: list[DocumentACL] = []
            for granted_user in rng.sample(users, k=rng.randint(0, 3)):
                acl_row = DocumentACL(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    principal_type="user",
                    principal_id=str(granted_user.id),
                    permission="read",
                )
                grants.append(acl_row)

            for granted_role in rng.sample(ROLES, k=rng.randint(0, 2)):
                acl_row = DocumentACL(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    principal_type="role",
                    principal_id=granted_role,
                    permission="read",
                )
                grants.append(acl_row)

            for granted_group in rng.sample(groups, k=rng.randint(0, 3)):
                acl_row = DocumentACL(
                    id=uuid.uuid4(),
                    document_id=doc.id,
                    principal_type="group",
                    principal_id=str(granted_group.id),
                    permission="read",
                )
                grants.append(acl_row)

            pending_grants.append((doc, grants))
            combinations.append((actor, doc, grants))

        await session.flush()
        for _doc, grants in pending_grants:
            for acl_row in grants:
                session.add(acl_row)

        await session.commit()

        # Verify SQL vs Python parity on each of the 200 combinations and on the full document set per user
        assert len(combinations) == 200
        sql_Cache: dict[uuid.UUID, set[str]] = {}
        stmt = text(
            f"""
            SELECT d.id::text
            FROM documents d
            WHERE d.deleted_at IS NULL
              AND d.status = 'ready'
              AND {acl_sql()}
            """
        )

        for idx, (actor, doc, grants) in enumerate(combinations):
            if actor.id not in sql_Cache:
                rows = await session.execute(
                    stmt,
                    {
                        "is_admin": actor.role == "admin",
                        "uid": str(actor.id),
                        "uid_text": str(actor.id),
                        "role": actor.role,
                    },
                )
                sql_Cache[actor.id] = {str(r[0]) for r in rows}

            principal = Principal(
                id=actor.id,
                role=actor.role,
                group_ids=frozenset(user_groups[actor.id]),
            )
            py_allowed = can_read(principal, doc, grants)
            sql_allowed = str(doc.id) in sql_Cache[actor.id]
            assert sql_allowed == py_allowed, (
                f"ACL mismatch at combination {idx}: "
                f"actor={actor.id} role={actor.role} groups={user_groups[actor.id]} "
                f"doc={doc.id} owner={doc.owner_id} vis={doc.visibility} "
                f"status={doc.status} deleted={doc.deleted_at is not None} "
                f"sql={sql_allowed} py={py_allowed}"
            )
