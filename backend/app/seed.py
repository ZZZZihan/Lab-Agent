"""Load synthetic lab documents and local demo accounts. No real personal data."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import select

from app.auth import hash_password
from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.models import Group, GroupMember, User
from app.rag.chunker import parse_front_matter
from app.rag.embedder import build_embedder
from app.rag.ingest import upsert_source

DEMO_USERS = (
    ("admin@lab.local", "沈昭", "admin", "LabAgent-Demo-2026-Admin"),
    ("member@lab.local", "林可", "member", "LabAgent-Demo-2026-Member"),
    ("zhou@lab.local", "周宁", "member", "LabAgent-Demo-2026-Zhou"),
    ("viewer@lab.local", "王敏", "viewer", "LabAgent-Demo-2026-Viewer"),
)


def _read_dir(path: Path) -> list[tuple[dict[str, str], str]]:
    records: list[tuple[dict[str, str], str]] = []
    if not path.is_dir():
        return records
    for file in sorted(path.glob("*.md")):
        meta, body = parse_front_matter(file.read_text(encoding="utf-8"))
        records.append((meta, body))
    return records


def assert_demo_seed_allowed(app_env: str) -> None:
    """Demo accounts are local fixtures. Production must provision its own users."""
    if app_env == "prod":
        raise SystemExit(
            "Refusing to load demo accounts when APP_ENV=prod. "
            "Create users out of band and do not reuse the LabAgent-Demo passwords."
        )


async def seed() -> None:
    settings = get_settings()
    assert_demo_seed_allowed(settings.app_env)
    embedder = build_embedder(settings)
    maker = get_sessionmaker()
    try:
        async with maker() as session:
            users: dict[str, User] = {}
            for email, name, role, password in DEMO_USERS:
                user = await session.scalar(select(User).where(User.email == email))
                if user is None:
                    user = User(
                        email=email,
                        display_name=name,
                        role=role,
                        password_hash=hash_password(password),
                        is_active=True,
                    )
                    session.add(user)
                    await session.flush()
                users[email] = user
            admin = users["admin@lab.local"]
            helios_group = await session.scalar(select(Group).where(Group.slug == "helios"))
            if helios_group is None:
                helios_group = Group(slug="helios", name="项目赫利俄斯组")
                session.add(helios_group)
                await session.flush()
            member_user = users["member@lab.local"]
            existing_membership = await session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == helios_group.id,
                    GroupMember.user_id == member_user.id,
                )
            )
            if existing_membership is None:
                session.add(
                    GroupMember(
                        group_id=helios_group.id,
                        user_id=member_user.id,
                    )
                )
                await session.flush()

            grants = {
                "procurement": [users["zhou@lab.local"].id],
            }
            group_grants = {
                "project-helios": [helios_group.id],
            }
            corpus = _read_dir(Path(settings.corpus_dir))
            private = _read_dir(Path(settings.seed_only_dir))
            stats = await upsert_source(
                session,
                embedder=embedder,
                owner_id=admin.id,
                source="seed",
                records=corpus + private,
                grants=grants,
                group_grants=group_grants,
            )
            await session.commit()
    finally:
        await embedder.aclose()
        await dispose_engine()
    print(f"seeded {stats.total} documents for 清衡实验室")


def main() -> None:
    asyncio.run(seed())


if __name__ == "__main__":
    main()
