"""FastAPI dependencies: DB session, authenticated user, role guards (ESD §8)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.security import ACCESS_COOKIE, decode_token
from core.db import get_sessionmaker
from db.enums import UserRole
from db.models import User
from providers.base import LLMProvider
from providers.factory import get_provider


async def get_session() -> AsyncIterator[AsyncSession]:
    """Per-request transactional session: commit on success, rollback on exception."""
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_current_user(request: Request, session: AsyncSession = Depends(get_session)) -> User:
    """Resolve the caller from the httpOnly access cookie (never a JS-readable store)."""
    token = request.cookies.get(ACCESS_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    claims = decode_token(token, expected_type="access")
    if claims is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    user = await session.get(User, uuid.UUID(claims["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return user


def require_role(*roles: UserRole):
    """Server-side role gate for state-changing endpoints (ESD §8 — never client-only)."""

    async def guard(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="insufficient role")
        return user

    return guard


def get_llm_provider() -> LLMProvider:
    """The configured LLM provider, as a dependency.

    A dependency rather than a direct `get_provider()` call so tests can override it:
    without this the resolve endpoint made a live model call inside the integration suite,
    which made it slow, non-deterministic, and dependent on upstream quota to pass.
    """
    return get_provider()
