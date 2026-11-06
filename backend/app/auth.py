"""Local JWT sessions, with an OIDC verification path that fails closed."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jwt import PyJWKClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.acl import load_user_group_ids
from app.config import Settings
from app.models import ApiToken, RevokedToken, User

_hasher = PasswordHasher()
_jwks_clients: dict[str, PyJWKClient] = {}
PAT_PREFIX = "la_pat_"
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID
    role: str
    display_name: str
    group_ids: frozenset[str] = frozenset()
    auth_via_pat: bool = False
    token_id: uuid.UUID | None = None


UNUSABLE_PASSWORD_HASH = "!unusable"
_DEMO_PREFIX = "labagent-demo-" + "2026-"


def validate_password(password: str, email: str | None = None) -> None:
    """Enforce password policy: >=12 chars, not a demo password, no email local part, >=3 char classes."""
    if len(password) < 12:
        raise ValueError("密码长度至少为 12 个字符")
    lowered = password.lower()
    if lowered.startswith(_DEMO_PREFIX):
        raise ValueError("不能使用演示账号密码")
    if email:
        local_part = email.split("@", 1)[0].strip().lower()
        if local_part and local_part in lowered:
            raise ValueError("密码不能包含邮箱的本地部分")
    classes = sum(
        (
            any(ch.islower() for ch in password),
            any(ch.isupper() for ch in password),
            any(ch.isdigit() for ch in password),
            any(not ch.isalnum() for ch in password),
        )
    )
    if classes < 3:
        raise ValueError("密码至少需包含大写字母、小写字母、数字和特殊字符中的 3 类")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if password_hash.startswith("!"):
        return False
    try:
        return bool(_hasher.verify(password_hash, password))
    except VerifyMismatchError:
        return False


def password_fingerprint(password_hash: str) -> str:
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:16]


def issue_token(user: User, settings: Settings) -> tuple[str, str, int]:
    now = datetime.now(UTC)
    ttl = settings.jwt_ttl_minutes * 60
    jti = uuid.uuid4().hex
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "jti": jti,
        "pwd": password_fingerprint(user.password_hash),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    return token, jti, ttl


async def revoke_token(session: AsyncSession, jti: str, exp: int) -> None:
    session.add(RevokedToken(jti=jti, expires_at=datetime.fromtimestamp(exp, UTC)))


async def is_revoked(session: AsyncSession, jti: str) -> bool:
    row = await session.get(RevokedToken, jti)
    return row is not None


async def authenticate_local(session: AsyncSession, token: str, settings: Settings) -> Actor:
    if settings.oidc_issuer:
        return await authenticate_oidc(session, token, settings)
    try:
        payload = jwt.decode(token, settings.jwt_secret.get_secret_value(), algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise PermissionError("invalid_token") from exc
    jti = str(payload.get("jti") or "")
    if not jti or await is_revoked(session, jti):
        raise PermissionError("invalid_token")
    user = await session.get(User, uuid.UUID(str(payload["sub"])))
    if user is None or not user.is_active:
        raise PermissionError("invalid_token")
    token_pwd = payload.get("pwd")
    cur_pwd = password_fingerprint(user.password_hash)
    if token_pwd is not None and token_pwd != cur_pwd:
        allow_key = f"allow:{jti[:24]}:{cur_pwd}"
        if await session.get(RevokedToken, allow_key) is None:
            raise PermissionError("invalid_token")
    group_ids = await load_user_group_ids(session, user.id)
    return Actor(
        id=user.id,
        role=user.role,
        display_name=user.display_name,
        group_ids=group_ids,
    )


async def authenticate_oidc(session: AsyncSession, token: str, settings: Settings) -> Actor:
    """Verify an OIDC access token. Without a reachable key set this refuses the request."""
    if not settings.oidc_issuer or not settings.oidc_audience:
        raise PermissionError("oidc_unconfigured")
    try:
        if settings.oidc_jwks_json:
            keys = json.loads(settings.oidc_jwks_json)
            header = jwt.get_unverified_header(token)
            key = None
            for item in keys.get("keys", []):
                if item.get("kid") == header.get("kid"):
                    key = jwt.PyJWK.from_dict(item).key
                    break
            if key is None and keys.get("keys"):
                key = jwt.PyJWK.from_dict(keys["keys"][0]).key
            if key is None:
                raise PermissionError("oidc_unavailable")
            payload = jwt.decode(
                token,
                key,
                algorithms=["RS256", "ES256"],
                audience=settings.oidc_audience,
                issuer=settings.oidc_issuer,
            )
        else:
            client = _jwks_clients.get(settings.oidc_issuer)
            if client is None:
                client = PyJWKClient(f"{settings.oidc_issuer.rstrip('/')}/.well-known/jwks.json")
                _jwks_clients[settings.oidc_issuer] = client
            signing = client.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing.key,
                algorithms=["RS256", "ES256"],
                audience=settings.oidc_audience,
                issuer=settings.oidc_issuer,
            )
    except PermissionError:
        raise
    except Exception as exc:
        raise PermissionError("oidc_unavailable") from exc
    subject = str(payload.get("sub") or "").strip()
    email = str(payload.get("email") or "").strip()
    if not subject:
        raise PermissionError("oidc_unavailable")
    candidate_login = str(
        payload.get("preferred_username")
        or payload.get("employee_number")
        or payload.get("student_id")
        or subject
    ).strip()
    external_email = email or f"{subject}@oidc.local"
    user = await session.scalar(
        select(User).where(func.lower(User.email) == external_email.lower())
    )
    if user is None and candidate_login:
        user = await session.scalar(
            select(User).where(func.lower(User.login_name) == candidate_login.lower())
        )
    if user is None:
        raise PermissionError("oidc_user_not_provisioned")
    if not user.is_active:
        raise PermissionError("invalid_token")
    group_ids = await load_user_group_ids(session, user.id)
    return Actor(
        id=user.id,
        role=user.role,
        display_name=user.display_name,
        group_ids=group_ids,
    )


def decode_unverified_for_logout(token: str) -> tuple[str, int] | None:
    try:
        payload = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    jti = payload.get("jti")
    exp = payload.get("exp")
    if not jti or not exp:
        return None
    return str(jti), int(exp)


def hash_pat(raw_token: str) -> str:
    """Return the lowercase SHA-256 hex digest of a Personal Access Token."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_pat() -> tuple[str, str, str]:
    """Generate a Personal Access Token `la_pat_<base62(32-bytes)>`.

    Returns `(raw_token, token_hash, prefix)`.
    """
    num = int.from_bytes(secrets.token_bytes(32), "big")
    chars: list[str] = []
    while num > 0:
        num, rem = divmod(num, 62)
        chars.append(_BASE62[rem])
    encoded = "".join(reversed(chars)).rjust(43, "0")
    raw_token = f"{PAT_PREFIX}{encoded}"
    return raw_token, hash_pat(raw_token), raw_token[:15]


async def authenticate_pat(session: AsyncSession, raw_token: str) -> Actor:
    """Authenticate a `la_pat_...` Personal Access Token and update `last_used_at`."""
    if not raw_token.startswith(PAT_PREFIX):
        raise PermissionError("invalid_token")
    digest = hash_pat(raw_token)
    api_token = await session.scalar(select(ApiToken).where(ApiToken.token_hash == digest))
    if api_token is None or api_token.revoked_at is not None:
        raise PermissionError("invalid_token")
    now = datetime.now(UTC)
    if api_token.expires_at is not None and api_token.expires_at <= now:
        raise PermissionError("invalid_token")
    user = await session.get(User, api_token.user_id)
    if user is None or not user.is_active:
        raise PermissionError("invalid_token")
    api_token.last_used_at = now
    await session.commit()
    group_ids = await load_user_group_ids(session, user.id)
    return Actor(
        id=user.id,
        role=user.role,
        display_name=user.display_name,
        group_ids=group_ids,
        auth_via_pat=True,
        token_id=api_token.id,
    )

