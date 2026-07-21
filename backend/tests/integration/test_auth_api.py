"""Integration tests: Firebase session exchange, cookie policy, rotation, RBAC (ESD §8).

Google's signature verification is the only thing doubled here. Everything the exchange does
*after* verification — provisioning, allowlist role resolution, cookie issuance, rotation and
reuse detection — runs for real against the test database.
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes import auth as auth_routes
from core.config import get_settings
from db.models import User
from tests.support.auth import fake_identity

SESSION_URL = "/api/v1/auth/session"


@pytest.fixture
def verified(monkeypatch):
    """Make the exchange accept a chosen identity (or reject it, when given None)."""

    def _install(identity):
        async def _verify(_token: str):
            return identity

        monkeypatch.setattr(auth_routes, "verify_google_id_token", _verify)

    return _install


@pytest.fixture
def allowlist(monkeypatch):
    """Point the role allowlists at explicit values for the duration of one test."""

    def _install(*, admins: str = "", approvers: str = "") -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "aegis_admin_emails", admins)
        monkeypatch.setattr(settings, "aegis_approver_emails", approvers)

    return _install


async def test_exchange_provisions_user_and_sets_httponly_cookies(
    api_client: httpx.AsyncClient, session: AsyncSession, verified, allowlist
):
    allowlist(approvers="oncall@example.com")
    verified(fake_identity("oncall@example.com", display_name="On Call"))

    response = await api_client.post(SESSION_URL, json={"id_token": "any-token"})
    assert response.status_code == 200
    assert response.json()["role"] == "on_call_engineer"

    # The session credential must never be reachable from JavaScript (CLAUDE.md §12).
    set_cookie = ";".join(response.headers.get_list("set-cookie")).lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie

    me = await api_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["display_name"] == "On Call"

    user = (
        await session.execute(select(User).where(User.email == "oncall@example.com"))
    ).scalar_one()
    assert user.firebase_uid == "uid-oncall@example.com"
    assert user.hashed_password is None  # a federated user never has one
    assert user.last_login_at is not None


async def test_unlisted_email_is_provisioned_viewer(
    api_client: httpx.AsyncClient, verified, allowlist
):
    """The load-bearing property: authentication is open, authorization is not."""
    allowlist(admins="boss@example.com", approvers="oncall@example.com")
    verified(fake_identity("random.person@gmail.com"))

    response = await api_client.post(SESSION_URL, json={"id_token": "any-token"})
    assert response.status_code == 200
    assert response.json()["role"] == "viewer"


async def test_admin_allowlist_wins_over_approver(
    api_client: httpx.AsyncClient, verified, allowlist
):
    allowlist(admins="both@example.com", approvers="both@example.com")
    verified(fake_identity("both@example.com"))
    response = await api_client.post(SESSION_URL, json={"id_token": "any-token"})
    assert response.json()["role"] == "admin"


async def test_role_is_re_resolved_on_every_sign_in(
    api_client: httpx.AsyncClient, session: AsyncSession, verified, allowlist
):
    """Revoking an approver takes effect at their next login, with no database edit."""
    verified(fake_identity("temp@example.com"))
    allowlist(approvers="temp@example.com")
    first = await api_client.post(SESSION_URL, json={"id_token": "any-token"})
    assert first.json()["role"] == "on_call_engineer"

    allowlist(approvers="")  # removed from the allowlist
    second = await api_client.post(SESSION_URL, json={"id_token": "any-token"})
    assert second.json()["role"] == "viewer"

    users = (
        (await session.execute(select(User).where(User.email == "temp@example.com")))
        .scalars()
        .all()
    )
    assert len(users) == 1  # re-sign-in updates the row, never duplicates it


async def test_rejected_token_yields_401_and_provisions_nothing(
    api_client: httpx.AsyncClient, session: AsyncSession, verified
):
    verified(None)
    response = await api_client.post(SESSION_URL, json={"id_token": "forged"})
    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"
    assert (await session.execute(select(User))).scalars().all() == []


async def test_refresh_rotation_and_reuse_detection(
    api_client: httpx.AsyncClient, verified, allowlist
):
    allowlist()
    verified(fake_identity("r@example.com"))
    await api_client.post(SESSION_URL, json={"id_token": "any-token"})
    old_refresh = api_client.cookies.get("aegis_refresh")

    # First refresh: rotates.
    r1 = await api_client.post("/api/v1/auth/refresh")
    assert r1.status_code == 200
    new_refresh = api_client.cookies.get("aegis_refresh")
    assert new_refresh != old_refresh

    # Replay the OLD token: theft signal → whole family revoked.
    api_client.cookies.set("aegis_refresh", old_refresh, path="/api/v1/auth")
    r2 = await api_client.post("/api/v1/auth/refresh")
    assert r2.status_code == 401

    # Even the newer (legitimately rotated) token is now dead.
    api_client.cookies.set("aegis_refresh", new_refresh, path="/api/v1/auth")
    r3 = await api_client.post("/api/v1/auth/refresh")
    assert r3.status_code == 401


async def test_logout_clears_cookies(api_client: httpx.AsyncClient, verified, allowlist):
    allowlist()
    verified(fake_identity("bye@example.com"))
    await api_client.post(SESSION_URL, json={"id_token": "any-token"})
    assert (await api_client.get("/api/v1/auth/me")).status_code == 200

    assert (await api_client.post("/api/v1/auth/logout")).status_code == 204
    api_client.cookies.clear()
    assert (await api_client.get("/api/v1/auth/me")).status_code == 401


async def test_incident_list_requires_auth(api_client: httpx.AsyncClient):
    response = await api_client.get("/api/v1/incidents")
    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"
