"""Test-only helpers for authenticating an API client (ESD §8).

Aegis has no password login, so a test that needs an authenticated caller cannot simply POST
credentials. Two supported paths:

* ``authenticate_as`` mints the session cookie directly. Use it in tests whose subject is some
  *other* endpoint and for which sign-in is only a precondition — it keeps them independent of
  Google entirely.
* ``fake_identity`` supplies a verified-identity double for tests whose subject IS the exchange.
  It stands in for Google's signature verification only; everything downstream of verification
  (provisioning, allowlist role resolution, cookie issuance) still runs for real.

Per CLAUDE.md §18 this module lives under ``tests/`` and is unreachable from runtime code.
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from api.firebase_auth import FirebaseIdentity
from api.security import ACCESS_COOKIE, create_access_token
from db.enums import UserRole
from db.models import User


def fake_identity(
    email: str,
    *,
    uid: str | None = None,
    display_name: str | None = "Test User",
    photo_url: str | None = None,
) -> FirebaseIdentity:
    """A verified identity as ``verify_google_id_token`` would return it."""
    return FirebaseIdentity(
        uid=uid or f"uid-{email}",
        email=email.strip().lower(),
        email_verified=True,
        display_name=display_name,
        photo_url=photo_url,
    )


async def authenticate_as(
    client: httpx.AsyncClient,
    session: AsyncSession,
    *,
    email: str,
    role: UserRole,
) -> User:
    """Create a user and attach a valid access cookie to ``client``.

    The cookie is minted with the same ``create_access_token`` the real exchange uses, so the
    request path under test is identical to production from the dependency layer down.
    """
    user = User(email=email.strip().lower(), firebase_uid=f"uid-{email}", role=role)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    client.cookies.set(
        ACCESS_COOKIE,
        create_access_token(user_id=str(user.id), role=user.role, email=user.email),
    )
    return user
