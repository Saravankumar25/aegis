"""Auth primitives: JWT issuance/verification, cookie policy (ESD §8).

Aegis issues and owns the session even though identity is federated through Firebase
(``api.firebase_auth``). Tokens live exclusively in httpOnly cookies, never anywhere
JavaScript can read them (CLAUDE.md §12). Access tokens are short-lived; refresh tokens are
opaque-to-the-client JWTs whose ``jti`` is stored hashed in ``refresh_sessions`` for rotation
and reuse detection.

There are no password primitives here: Aegis never sees, stores, or verifies a password.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from typing import Any

from fastapi import Response
from jose import JWTError, jwt

from core.config import get_settings

ALGORITHM = "HS256"
ACCESS_COOKIE = "aegis_access"
REFRESH_COOKIE = "aegis_refresh"
REFRESH_PATH = "/api/v1/auth"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def create_access_token(*, user_id: str, role: str, email: str) -> str:
    """Short-lived access JWT (default 15 min, ESD §8)."""
    settings = get_settings()
    claims = {
        "sub": user_id,
        "role": role,
        "email": email,
        "type": "access",
        "exp": _now() + datetime.timedelta(seconds=settings.jwt_access_ttl_seconds),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)


def create_refresh_token(*, user_id: str, family_id: str) -> tuple[str, str, datetime.datetime]:
    """Refresh JWT. Returns ``(token, jti_hash, expires_at)`` — only the hash is stored."""
    settings = get_settings()
    jti = uuid.uuid4().hex
    expires_at = _now() + datetime.timedelta(seconds=settings.jwt_refresh_ttl_seconds)
    claims = {
        "sub": user_id,
        "jti": jti,
        "family": family_id,
        "type": "refresh",
        "exp": expires_at,
    }
    token = jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM)
    return token, hash_token_id(jti), expires_at


def hash_token_id(jti: str) -> str:
    """SHA-256 of the token id; the raw jti never touches the database."""
    return hashlib.sha256(jti.encode()).hexdigest()


def decode_token(token: str, *, expected_type: str) -> dict[str, Any] | None:
    """Verify signature + expiry + type. Returns claims or None (never raises to callers)."""
    try:
        claims = jwt.decode(token, get_settings().jwt_secret, algorithms=[ALGORITHM])
    except JWTError:
        return None
    if claims.get("type") != expected_type:
        return None
    return claims


def set_auth_cookies(response: Response, *, access_token: str, refresh_token: str) -> None:
    """Attach both tokens as httpOnly cookies (Secure, SameSite=Strict — ESD §8).

    ``secure`` is relaxed only for the local (http://localhost) environment; any deployed
    environment keeps it on.
    """
    settings = get_settings()
    secure = settings.environment not in ("local", "test")
    common: dict[str, Any] = {"httponly": True, "secure": secure, "samesite": "strict"}
    response.set_cookie(
        ACCESS_COOKIE, access_token, max_age=settings.jwt_access_ttl_seconds, **common
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        max_age=settings.jwt_refresh_ttl_seconds,
        path=REFRESH_PATH,
        **common,
    )


def clear_auth_cookies(response: Response) -> None:
    """Remove both auth cookies (logout / family revocation)."""
    response.delete_cookie(ACCESS_COOKIE)
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_PATH)
