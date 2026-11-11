"""Command-line user administration (`python -m app.admin_cli`)."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import sys
from collections.abc import Sequence

from sqlalchemy import func, or_, select

from app.audit import verify_audit_chain, write_audit
from app.auth import hash_password, validate_password
from app.config import get_settings
from app.db import dispose_engine, get_sessionmaker
from app.models import User

ROLES = ("admin", "member", "viewer")


def _read_password_stdin() -> str:
    return sys.stdin.read().rstrip("\r\n")


async def _run(args: argparse.Namespace, password: str | None) -> int:
    settings = get_settings()
    maker = get_sessionmaker()
    try:
        async with maker() as session:
            cmd = str(args.command)
            if cmd == "list-users":
                rows = list(
                    await session.scalars(
                        select(User).order_by(User.created_at.asc(), User.email.asc())
                    )
                )
                for user in rows:
                    status = "active" if user.is_active else "inactive"
                    created = user.created_at.isoformat() if user.created_at else ""
                    ident = user.email or user.login_name or str(user.id)
                    print(
                        f"{user.id}\t{ident}\t{user.display_name}\t{user.role}\t{status}\t{created}"
                    )
                return 0

            if cmd == "verify-audit":
                broken_id = await verify_audit_chain(session)
                if broken_id is not None:
                    print(f"audit chain broken at id={broken_id}")
                    return 1
                print("audit chain ok")
                return 0

            email = str(args.email).strip().lower()
            if not email:
                print("Error: invalid email address", file=sys.stderr)
                return 1

            if cmd == "create-user":
                if "@" not in email:
                    print("Error: invalid email address", file=sys.stderr)
                    return 1
                name = str(args.name).strip()
                role = str(args.role).strip()
                if not name:
                    print("Error: display name cannot be empty", file=sys.stderr)
                    return 1
                if password is None:
                    print("Error: --password-stdin is required", file=sys.stderr)
                    return 1
                try:
                    validate_password(password, email)
                except ValueError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    return 1
                existing = await session.scalar(select(User).where(User.email == email))
                if existing is not None:
                    print(f"Error: user already exists: {email}", file=sys.stderr)
                    return 1
                user = User(
                    email=email,
                    display_name=name,
                    role=role,
                    password_hash=hash_password(password),
                    is_active=True,
                )
                session.add(user)
                await session.flush()
                user_id = str(user.id)
                await write_audit(
                    settings=settings,
                    actor_id=None,
                    action="user_created",
                    resource_type="user",
                    resource_id=user_id,
                    detail={"role": role, "via": "cli"},
                    session=session,
                )
                await session.commit()
                print(f"created {user_id} {email} ({role})")
                return 0

            target_user = await session.scalar(
                select(User).where(
                    or_(
                        func.lower(User.email) == email,
                        func.lower(User.login_name) == email,
                    )
                )
            )
            if target_user is None:
                print(f"Error: user not found: {email}", file=sys.stderr)
                return 1
            user_id = str(target_user.id)

            if cmd == "set-role":
                new_role = str(args.role).strip()
                old_role = target_user.role
                target_user.role = new_role
                await write_audit(
                    settings=settings,
                    actor_id=None,
                    action="user_updated",
                    resource_type="user",
                    resource_id=user_id,
                    detail={
                        "fields": ["role"],
                        "role_from": old_role,
                        "role_to": new_role,
                        "via": "cli",
                    },
                    session=session,
                )
                await session.commit()
                print(f"updated role {user_id} {old_role} -> {new_role}")
                return 0

            if cmd in {"deactivate", "activate"}:
                target_user.is_active = cmd == "activate"
                await write_audit(
                    settings=settings,
                    actor_id=None,
                    action="user_updated",
                    resource_type="user",
                    resource_id=user_id,
                    detail={"fields": ["is_active"], "via": "cli"},
                    session=session,
                )
                await session.commit()
                state_str = "active" if target_user.is_active else "inactive"
                print(f"updated status {user_id} {state_str}")
                return 0

            if cmd == "reset-password":
                if password is None:
                    print("Error: --password-stdin is required", file=sys.stderr)
                    return 1
                try:
                    validate_password(
                        password, target_user.email or target_user.login_name
                    )
                except ValueError as exc:
                    print(f"Error: {exc}", file=sys.stderr)
                    return 1
                target_user.password_hash = hash_password(password)
                target_user.must_change_password_by = None
                await write_audit(
                    settings=settings,
                    actor_id=None,
                    action="user_password_reset",
                    resource_type="user",
                    resource_id=user_id,
                    detail={"via": "cli"},
                    session=session,
                )
                await session.commit()
                print(f"reset password {user_id}")
                return 0

            print(f"Error: unknown command {cmd}", file=sys.stderr)
            return 2
    finally:
        await dispose_engine()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Lab Agent user administration CLI.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create-user", help="Create a new user account")
    p_create.add_argument("--email", required=True, help="User email address")
    p_create.add_argument("--name", required=True, help="Display name")
    p_create.add_argument("--role", required=True, choices=ROLES, help="User role")
    p_create.add_argument(
        "--password-stdin",
        action="store_true",
        required=True,
        help="Read password from stdin",
    )

    p_role = sub.add_parser("set-role", help="Change a user's role")
    p_role.add_argument("--email", required=True, help="User email address")
    p_role.add_argument("--role", required=True, choices=ROLES, help="Target role")

    p_deact = sub.add_parser("deactivate", help="Deactivate a user account")
    p_deact.add_argument("--email", required=True, help="User email address")

    p_act = sub.add_parser("activate", help="Activate a user account")
    p_act.add_argument("--email", required=True, help="User email address")

    p_reset = sub.add_parser("reset-password", help="Reset a user's password")
    p_reset.add_argument("--email", required=True, help="User email address")
    p_reset.add_argument(
        "--password-stdin",
        action="store_true",
        required=True,
        help="Read password from stdin",
    )

    sub.add_parser("list-users", help="List all users without password hashes")
    sub.add_parser("verify-audit", help="Verify audit_events hash chain integrity")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else 2

    password: str | None = None
    if getattr(args, "password_stdin", False):
        password = _read_password_stdin()

    try:
        asyncio.get_running_loop()
        has_loop = True
    except RuntimeError:
        has_loop = False

    if has_loop:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(_run(args, password))).result()
    return asyncio.run(_run(args, password))


if __name__ == "__main__":
    sys.exit(main())
