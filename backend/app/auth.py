"""Local JWT sessions, with an OIDC verification path that fails closed."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import RevokedToken, User

_hasher = PasswordHasher()
_jwks_clients: dict[str, PyJWKClient] = {}


@dataclass(frozen=True)
class Actor:
    id: uuid.UUID
    role: str
    display_name: str


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return bool(_hasher.verify(password_hash, password))
    except VerifyMismatchError:
        return False


def issue_token(user: User, settings: Settings) -> tuple[str, str, int]:
    now = datetime.now(UTC)
    ttl = settings.jwt_ttl_minutes * 60
    jti = uuid.uuid4().hex
    payload = {
        "sub": str(user.id),
        "role": user.role,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl)).timestamp()),
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
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
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise PermissionError("invalid_token") from exc
    jti = str(payload.get("jti") or "")
    if not jti or await is_revoked(session, jti):
        raise PermissionError("invalid_token")
    user = await session.get(User, uuid.UUID(str(payload["sub"])))
    if user is None or not user.is_active:
        raise PermissionError("invalid_token")
    return Actor(id=user.id, role=user.role, display_name=user.display_name)


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
    subject = str(payload.get("sub") or "")
    email = str(payload.get("email") or "")
    if not subject:
        raise PermissionError("oidc_unavailable")
    external_email = email or f"{subject}@oidc.local"
    user = await session.scalar(select(User).where(User.email == external_email))
    if user is None:
        raise PermissionError("oidc_user_not_provisioned")
    if not user.is_active:
        raise PermissionError("invalid_token")
    return Actor(id=user.id, role=user.role, display_name=user.display_name)


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
