"""Auth routes: Firebase session exchange, refresh (rotation + reuse detection), me (ESD §7, §8).

Aegis does not authenticate passwords. A Google identity is proven to Firebase in the browser,
and the resulting ID token is exchanged **once** here for Aegis's own httpOnly session cookie.
After that exchange the frontend signs out of the Firebase client SDK, so no credential remains
anywhere JavaScript can read (CLAUDE.md §12).
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_session
from api.firebase_auth import FirebaseConfigError, resolve_role, verify_google_id_token
from api.schemas import SessionExchangeIn, UserOut
from api.security import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_token_id,
    set_auth_cookies,
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


def _auth_reject(message: str) -> JSONResponse:
    """401 envelope with cookies cleared — a raise would discard the cookie mutations."""
    response = JSONResponse(
        status_code=401,
        content={"error_code": "unauthorized", "message": message, "incident_id": None},
    )
    clear_auth_cookies(response)
    return response


async def _provision(session: AsyncSession, identity) -> User:
    """Find-or-create the Aegis user behind a verified Google identity.

    Matching is by ``firebase_uid`` first (stable across an email change) and by email
    second, which is what links a pre-existing Aegis account to its Google identity the
    first time that person signs in federated.

    The role is re-derived from the allowlist on **every** sign-in rather than only at
    creation, so revoking an approver is an env change plus their next login, not a manual
    database edit. It is never read from the token.
    """
    identity_role = resolve_role(identity.email)
    user = (
        await session.execute(select(User).where(User.firebase_uid == identity.uid))
    ).scalar_one_or_none()
    if user is None:
        user = (
            await session.execute(select(User).where(User.email == identity.email))
        ).scalar_one_or_none()
        if user is not None:
            user.firebase_uid = identity.uid

    if user is None:
        user = User(
            email=identity.email,
            firebase_uid=identity.uid,
            display_name=identity.display_name,
            photo_url=identity.photo_url,
            role=identity_role,
        )
        session.add(user)
        await session.flush()  # assign the PK before it is embedded in the access token
        get_logger(component="auth").info(
            "user_provisioned", user_id=str(user.id), role=identity_role
        )
    else:
        if user.role != identity_role:
            get_logger(component="auth").info(
                "user_role_changed",
                user_id=str(user.id),
                previous=user.role,
                current=identity_role,
            )
        user.email = identity.email
        user.role = identity_role
        user.display_name = identity.display_name
        user.photo_url = identity.photo_url

    user.last_login_at = _now()
    return user


@router.post("/session", response_model=UserOut)
async def create_session(
    body: SessionExchangeIn, response: Response, session: AsyncSession = Depends(get_session)
):
    """Exchange a verified Firebase ID token for an Aegis httpOnly session (ESD §8).

    This is the only entry point that mints a session. The ID token is verified server-side
    against Google's signing keys and this project's audience; nothing in the request body is
    trusted as identity.
    """
    try:
        identity = await verify_google_id_token(body.id_token)
    except FirebaseConfigError as exc:
        # A deployment problem, not a credential problem. Say so plainly rather than
        # returning 401 and sending the user off to re-check a password they do not have.
        get_logger(component="auth").error("firebase_not_configured", reason=str(exc))
        return JSONResponse(
            status_code=503,
            content={
                "error_code": "auth_unavailable",
                "message": "authentication is not configured on this server",
                "incident_id": None,
            },
        )

    if identity is None:
        return _auth_reject("invalid or expired sign-in")

    user = await _provision(session, identity)
    await _issue_session(session, response, user, family_id=uuid.uuid4())
    return user


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
