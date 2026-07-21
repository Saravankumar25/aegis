"""Auth routes: login, refresh (rotation + reuse detection), me (ESD §7, §8)."""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_session
from api.schemas import LoginIn, UserOut
from api.security import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_token_id,
    set_auth_cookies,
    verify_password,
)
from core.logging import get_logger
from db.models import RefreshSession, User

router = APIRouter(prefix="/auth", tags=["auth"])


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def _issue_session(
    session: AsyncSession, response: Response, user: User, family_id: uuid.UUID
) -> None:
    access = create_access_token(user_id=str(user.id), role=user.role, email=user.email)
    refresh, jti_hash, expires_at = create_refresh_token(
        user_id=str(user.id), family_id=str(family_id)
    )
    session.add(
        RefreshSession(
            user_id=user.id, token_hash=jti_hash, family_id=family_id, expires_at=expires_at
        )
    )
    set_auth_cookies(response, access_token=access, refresh_token=refresh)


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginIn, response: Response, session: AsyncSession = Depends(get_session)
) -> User:
    """Verify credentials and start a new refresh-token family."""
    user = (
        await session.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none()
    # Same error for unknown email and wrong password: no account enumeration.
    if user is None or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    await _issue_session(session, response, user, family_id=uuid.uuid4())
    return user


def _auth_reject(message: str) -> JSONResponse:
    """401 envelope with cookies cleared — a raise would discard the cookie mutations."""
    response = JSONResponse(
        status_code=401,
        content={"error_code": "unauthorized", "message": message, "incident_id": None},
    )
    clear_auth_cookies(response)
    return response


@router.post("/refresh", response_model=UserOut)
async def refresh(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
):
    """Rotate the refresh token; replay of an already-rotated token revokes the family."""
    token = request.cookies.get(REFRESH_COOKIE)
    claims = decode_token(token, expected_type="refresh") if token else None
    if claims is None:
        return _auth_reject("invalid refresh token")

    jti_hash = hash_token_id(claims["jti"])
    row = (
        await session.execute(select(RefreshSession).where(RefreshSession.token_hash == jti_hash))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at <= _now():
        return _auth_reject("invalid refresh token")

    if row.rotated_at is not None:
        # Reuse of a rotated token = theft signal (ESD §8): kill the whole family.
        await session.execute(
            update(RefreshSession)
            .where(RefreshSession.family_id == row.family_id)
            .values(revoked_at=_now())
        )
        await session.commit()  # the revocation must land even though we 401
        get_logger(component="auth").warning(
            "refresh_token_reuse_detected", family_id=str(row.family_id)
        )
        return _auth_reject("session revoked")

    user = await session.get(User, row.user_id)
    if user is None:
        return _auth_reject("unknown user")

    row.rotated_at = _now()
    await _issue_session(session, response, user, family_id=row.family_id)
    return user


@router.post("/logout", status_code=204)
async def logout(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> None:
    """Revoke the presented refresh family and clear cookies."""
    token = request.cookies.get(REFRESH_COOKIE)
    claims = decode_token(token, expected_type="refresh") if token else None
    if claims is not None:
        row = (
            await session.execute(
                select(RefreshSession).where(
                    RefreshSession.token_hash == hash_token_id(claims["jti"])
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            await session.execute(
                update(RefreshSession)
                .where(RefreshSession.family_id == row.family_id)
                .values(revoked_at=_now())
            )
    clear_auth_cookies(response)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    """Current authenticated user + role (the frontend's only identity source, ESD §5)."""
    return user
