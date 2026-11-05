"""Tests for seed GroupMember idempotency and Document ACL FOR UPDATE row lock."""

from __future__ import annotations

import asyncio

from app.auth import issue_token
from app.config import get_settings
from app.db import get_sessionmaker
from app.models import Document, Group, GroupMember, User
from app.seed import seed
from httpx import AsyncClient
from sqlalchemy import select, text


async def test_seed_preserves_existing_group_members() -> None:
    await seed()

    maker = get_sessionmaker()
    async with maker() as session:
        helios = await session.scalar(select(Group).where(Group.slug == "helios"))
        zhou = await session.scalar(select(User).where(User.email == "zhou@lab.local"))
        linke = await session.scalar(select(User).where(User.email == "member@lab.local"))
        assert helios is not None
        assert zhou is not None
        assert linke is not None

        session.add(GroupMember(group_id=helios.id, user_id=zhou.id))
        await session.commit()

    # Re-running seed() must be idempotent and preserve admin-added group members
    await seed()

    maker = get_sessionmaker()
    async with maker() as session:
        helios = await session.scalar(select(Group).where(Group.slug == "helios"))
        assert helios is not None
        member_emails = set(
            await session.scalars(
                select(User.email)
                .join(GroupMember, GroupMember.user_id == User.id)
                .where(GroupMember.group_id == helios.id)
            )
        )
        assert "member@lab.local" in member_emails
        assert "zhou@lab.local" in member_emails


async def test_update_document_acl_acquires_for_update_row_lock(
    client: AsyncClient,
) -> None:
    await seed()
    settings = get_settings()
    maker = get_sessionmaker()

    async with maker() as session:
        admin = await session.scalar(select(User).where(User.email == "admin@lab.local"))
        doc = await session.scalar(
            select(Document).where(Document.source == "seed", Document.doc_key == "procurement")
        )
        assert admin is not None
        assert doc is not None
        token, _, _ = issue_token(admin, settings)
        doc_id = doc.id

    headers = {"Authorization": f"Bearer {token}"}

    # Hold a FOR SHARE lock on the document in a separate transaction; a FOR UPDATE
    # query in update_document_acl must block until our transaction commits/rolls back.
    lock_acquired = asyncio.Event()
    release_lock = asyncio.Event()
    acl_completed = False

    async def _hold_row_share_lock() -> None:
        async with maker() as lock_session:
            await lock_session.execute(
                text("SELECT id FROM documents WHERE id = CAST(:id AS uuid) FOR SHARE"),
                {"id": str(doc_id)},
            )
            lock_acquired.set()
            await release_lock.wait()
            await lock_session.rollback()

    holder_task = asyncio.create_task(_hold_row_share_lock())
    await lock_acquired.wait()

    async def _call_update_acl() -> dict[str, object]:
        nonlocal acl_completed
        resp = await client.put(
            f"/api/v1/documents/{doc_id}/acl",
            headers=headers,
            json={
                "visibility": "restricted",
                "user_emails": ["member@lab.local", "zhou@lab.local"],
                "group_slugs": ["helios"],
                "roles": ["member"],
            },
        )
        assert resp.status_code == 200
        acl_completed = True
        return resp.json()

    update_task = asyncio.create_task(_call_update_acl())
    await asyncio.sleep(0.15)
    # While the FOR SHARE row lock is held, update_document_acl's SELECT ... FOR UPDATE waits
    assert acl_completed is False

    release_lock.set()
    payload = await update_task
    await holder_task

    assert acl_completed is True
    acl_info = payload["acl"]
    assert isinstance(acl_info, dict)
    assert acl_info["visibility"] == "restricted"
    assert set(acl_info["user_emails"]) == {"member@lab.local", "zhou@lab.local"}
    assert acl_info["group_slugs"] == ["helios"]
    assert acl_info["roles"] == ["member"]
