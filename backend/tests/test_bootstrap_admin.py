"""Tests for first-admin bootstrap status and initialization (/api/v1/auth/bootstrap-*)."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest
from app.auth import hash_password
from app.db import get_sessionmaker
from app.main import app
from app.models import AuditEvent, User
from app.routes_auth_bootstrap import ensure_prod_bootstrap_token
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select, update

STRONG_PW = "FirstAdmin!2026#Safe"


async def _insert_user(
    *,
    email: str,
    role: str = "admin",
    display_name: str = "沈昭",
    login_name: str | None = None,
    is_active: bool = True,
) -> User:
    maker = get_sessionmaker()
    async with maker() as session:
        user = User(
            email=email,
            login_name=login_name,
            display_name=display_name,
            role=role,
            password_hash=hash_password(STRONG_PW),
            is_active=is_active,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def test_bootstrap_closed_when_active_admin_exists(client: AsyncClient) -> None:
    await _insert_user(email="admin@lab.local", role="admin", is_active=True)

    status_res = await client.get("/api/v1/auth/bootstrap-status")
    assert status_res.status_code == 200
    assert status_res.json() == {
        "needs_bootstrap": False,
        "setup_token_required": False,
        "oidc_mode": False,
    }

    post_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "攻击者",
            "email": "attacker@example.edu.cn",
            "password": STRONG_PW,
        },
    )
    assert post_res.status_code == 409
    assert post_res.json()["error"]["code"] == "bootstrap_closed"


async def test_bootstrap_permanently_closed_when_admin_deactivated(
    client: AsyncClient,
) -> None:
    """P0 security requirement: deactivating all admins must NOT re-open bootstrap."""
    await _insert_user(email="admin@lab.local", role="admin", is_active=True)
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(update(User).where(User.role == "admin").values(is_active=False))
        await session.commit()

    status_res = await client.get("/api/v1/auth/bootstrap-status")
    assert status_res.status_code == 200
    assert status_res.json()["needs_bootstrap"] is False

    post_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "抢注管理员",
            "email": "hijack@example.edu.cn",
            "password": STRONG_PW,
        },
    )
    assert post_res.status_code == 409
    assert post_res.json()["error"]["code"] == "bootstrap_closed"


async def test_bootstrap_succeeds_when_no_admin_exists(client: AsyncClient) -> None:
    # Ensure a non-admin member does not block first-admin bootstrap
    await _insert_user(
        email="member@lab.local",
        role="member",
        display_name="林可",
        login_name="linke001",
    )
    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(delete(User).where(User.role == "admin"))
        await session.commit()

    status_res = await client.get("/api/v1/auth/bootstrap-status")
    assert status_res.status_code == 200
    assert status_res.json() == {
        "needs_bootstrap": True,
        "setup_token_required": False,
        "oidc_mode": False,
    }

    post_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": " 郭 建 民 ",
            "email": f"  {'guojianmin@example.edu.cn'.upper()}  ",
            "staff_no": "TG109823",
            "password": STRONG_PW,
        },
    )
    assert post_res.status_code == 200, post_res.text
    payload = post_res.json()
    assert payload["token_type"] == "bearer"
    assert isinstance(payload["access_token"], str) and payload["access_token"]
    user_data = payload["user"]
    assert user_data["display_name"] == "郭建民"
    assert user_data["email"] == "guojianmin@example.edu.cn"
    assert user_data["login_name"] == "tg109823"
    assert user_data["role"] == "admin"
    assert user_data["must_change_password"] is False
    assert user_data["oidc_mode"] is False

    # Verify /api/v1/auth/me works with the issued token
    me_res = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
    )
    assert me_res.status_code == 200
    assert me_res.json()["id"] == user_data["id"]
    assert me_res.json()["role"] == "admin"
    assert me_res.json()["oidc_mode"] is False

    # Verify bootstrap is now closed
    status_after = await client.get("/api/v1/auth/bootstrap-status")
    assert status_after.status_code == 200
    assert status_after.json()["needs_bootstrap"] is False

    # Verify audit event has no PII in detail
    async with maker() as session:
        audit_row = await session.scalar(
            select(AuditEvent).where(AuditEvent.action == "bootstrap_admin")
        )
        assert audit_row is not None
        assert str(audit_row.actor_id) == user_data["id"]
        assert audit_row.resource_type == "user"
        assert audit_row.resource_id == user_data["id"]
        assert audit_row.detail == {"user_id": user_data["id"]}


async def test_bootstrap_concurrent_race_allows_only_one(client: AsyncClient) -> None:
    r1, r2 = await asyncio.gather(
        client.post(
            "/api/v1/auth/bootstrap-admin",
            json={
                "display_name": "管理员甲",
                "email": "admin1@example.edu.cn",
                "staff_no": "10000001",
                "password": STRONG_PW,
            },
        ),
        client.post(
            "/api/v1/auth/bootstrap-admin",
            json={
                "display_name": "管理员乙",
                "email": "admin2@example.edu.cn",
                "staff_no": "10000002",
                "password": STRONG_PW,
            },
        ),
    )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [200, 409]
    failed = r1 if r1.status_code == 409 else r2
    assert failed.json()["error"]["code"] == "bootstrap_closed"


async def test_bootstrap_prod_setup_token_and_file_generation(
    client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "sub" / "bootstrap_admin.token"
    settings = app.state.settings
    monkeypatch.setattr(settings, "app_env", "prod")
    monkeypatch.setattr(settings, "bootstrap_token", None)
    monkeypatch.setattr(settings, "bootstrap_token_file", str(token_file))

    maker = get_sessionmaker()
    async with maker() as session:
        created_path = await ensure_prod_bootstrap_token(session, settings)
        assert created_path == token_file
        assert token_file.is_file()
        mode = stat.S_IMODE(token_file.stat().st_mode)
        assert mode == 0o600
        generated_token = token_file.read_text(encoding="utf-8").strip()
        assert len(generated_token) >= 24

    status_res = await client.get("/api/v1/auth/bootstrap-status")
    assert status_res.status_code == 200
    assert status_res.json() == {
        "needs_bootstrap": True,
        "setup_token_required": True,
        "oidc_mode": False,
    }

    # Missing setup_token -> 403 setup_token_invalid
    missing_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "password": STRONG_PW,
        },
    )
    assert missing_res.status_code == 403
    assert missing_res.json()["error"]["code"] == "setup_token_invalid"

    # Wrong setup_token -> 403 setup_token_invalid
    wrong_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "password": STRONG_PW,
            "setup_token": "wrong-token-value",
        },
    )
    assert wrong_res.status_code == 403
    assert wrong_res.json()["error"]["code"] == "setup_token_invalid"

    # Matching setup_token -> 200 and token file unlinked
    ok_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "password": STRONG_PW,
            "setup_token": generated_token,
        },
    )
    assert ok_res.status_code == 200, ok_res.text
    assert not token_file.exists()


async def test_bootstrap_configured_env_token_in_dev(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_token = "test-qwdj-bootstrap-setup-2026"
    settings = app.state.settings
    monkeypatch.setattr(settings, "bootstrap_token", SecretStr(env_token))

    status_res = await client.get("/api/v1/auth/bootstrap-status")
    assert status_res.status_code == 200
    assert status_res.json()["setup_token_required"] is True

    bad_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "password": STRONG_PW,
            "setup_token": "mismatch",
        },
    )
    assert bad_res.status_code == 403
    assert bad_res.json()["error"]["code"] == "setup_token_invalid"

    ok_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "password": STRONG_PW,
            "setup_token": env_token,
        },
    )
    assert ok_res.status_code == 200


async def test_bootstrap_disabled_in_oidc_mode(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = app.state.settings
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.com")
    monkeypatch.setattr(settings, "oidc_audience", "lab-agent")

    status_res = await client.get("/api/v1/auth/bootstrap-status")
    assert status_res.status_code == 200
    assert status_res.json() == {
        "needs_bootstrap": False,
        "setup_token_required": False,
        "oidc_mode": True,
    }

    post_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "password": STRONG_PW,
        },
    )
    assert post_res.status_code == 403
    assert post_res.json()["error"]["code"] == "bootstrap_disabled"


async def test_bootstrap_validation_and_identity_conflicts(client: AsyncClient) -> None:
    await _insert_user(
        email="taken@example.edu.cn",
        role="member",
        display_name="已有成员",
        login_name="24311099",
    )

    # 18-digit ID card as staff_no -> 422 staff_no_invalid
    id_card_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "staff_no": "11010119900307123X",
            "password": STRONG_PW,
        },
    )
    assert id_card_res.status_code == 422
    assert id_card_res.json()["error"]["code"] == "staff_no_invalid"

    # 11-digit phone number as staff_no -> 422 staff_no_invalid
    phone_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "staff_no": "13800138000",
            "password": STRONG_PW,
        },
    )
    assert phone_res.status_code == 422
    assert phone_res.json()["error"]["code"] == "staff_no_invalid"

    # Reserved login name 'admin' -> 422 staff_no_invalid
    reserved_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "staff_no": "admin",
            "password": STRONG_PW,
        },
    )
    assert reserved_res.status_code == 422
    assert reserved_res.json()["error"]["code"] == "staff_no_invalid"

    # Weak password -> 422 password_policy_violation
    weak_pw_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "password": "short",
        },
    )
    assert weak_pw_res.status_code == 422
    assert weak_pw_res.json()["error"]["code"] == "password_policy_violation"

    # Extra unknown field -> 422
    extra_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "password": STRONG_PW,
            "role": "admin",
        },
    )
    assert extra_res.status_code == 422

    # Existing member email -> 409 identity_taken
    email_taken_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "taken@example.edu.cn",
            "password": STRONG_PW,
        },
    )
    assert email_taken_res.status_code == 409
    assert email_taken_res.json()["error"]["code"] == "identity_taken"

    # Existing member staff_no -> 409 identity_taken
    staff_taken_res = await client.post(
        "/api/v1/auth/bootstrap-admin",
        json={
            "display_name": "郭建民",
            "email": "pi@example.edu.cn",
            "staff_no": "24311099",
            "password": STRONG_PW,
        },
    )
    assert staff_taken_res.status_code == 409
    assert staff_taken_res.json()["error"]["code"] == "identity_taken"


async def test_bootstrap_rate_limit_uses_per_ip_bucket_not_shared_account(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rate limiting on bootstrap endpoints must be per-client IP, not a shared global account bucket."""
    import fakeredis.aioredis
    from app.config import get_settings

    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(delete(User).where(User.role == "admin"))
        await session.commit()

    settings = get_settings()
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(app.state.limiter, "redis", fake_redis)
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "internal_proxy_secret", SecretStr("proxy-secret-test"))
    monkeypatch.setattr(settings, "login_account_rate_limit", 2)
    monkeypatch.setattr(settings, "login_ip_rate_limit", 5)

    try:
        # 12 requests across 4 distinct client IPs (3 each <= ip limit 5, total 12 > account limit 2)
        for idx in range(12):
            ip_addr = f"203.0.113.{(idx % 4) + 10}"
            res = await client.get(
                "/api/v1/auth/bootstrap-status",
                headers={
                    "x-internal-proxy-secret": "proxy-secret-test",
                    "x-client-ip": ip_addr,
                },
            )
            assert res.status_code == 200, res.text
            assert res.json()["needs_bootstrap"] is True
    finally:
        await fake_redis.aclose()

