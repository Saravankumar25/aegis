"""Integration tests: JWT cookie auth — login, me, rotation, reuse detection (ESD §8)."""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from api.security import hash_password
from db.enums import UserRole
from db.models import User


async def _seed_user(session: AsyncSession, email: str, password: str, role: UserRole) -> None:
    session.add(User(email=email, hashed_password=hash_password(password), role=role))
    await session.commit()


async def test_login_sets_httponly_cookies_and_me_works(
    api_client: httpx.AsyncClient, session: AsyncSession
):
    await _seed_user(session, "oncall@example.com", "correct-horse-9", UserRole.on_call_engineer)
    response = await api_client.post(
        "/api/v1/auth/login", json={"email": "oncall@example.com", "password": "correct-horse-9"}
    )
    assert response.status_code == 200
    set_cookie = ";".join(response.headers.get_list("set-cookie")).lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie

    me = await api_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["role"] == "on_call_engineer"


async def test_wrong_password_and_unknown_email_same_error(
    api_client: httpx.AsyncClient, session: AsyncSession
):
    await _seed_user(session, "a@example.com", "correct-horse-9", UserRole.viewer)
    r1 = await api_client.post(
        "/api/v1/auth/login", json={"email": "a@example.com", "password": "wrong-password-1"}
    )
    r2 = await api_client.post(
        "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "wrong-password-1"}
    )
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["message"] == r2.json()["message"]  # no account enumeration


async def test_refresh_rotation_and_reuse_detection(
    api_client: httpx.AsyncClient, session: AsyncSession
):
    await _seed_user(session, "r@example.com", "correct-horse-9", UserRole.viewer)
    await api_client.post(
        "/api/v1/auth/login", json={"email": "r@example.com", "password": "correct-horse-9"}
    )
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


async def test_incident_list_requires_auth(api_client: httpx.AsyncClient):
    response = await api_client.get("/api/v1/incidents")
    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"
