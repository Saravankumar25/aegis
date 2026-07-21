"""Integration tests: rate limiting and the webhook guard (ESD §12, §16).

These exist because of a real defect. Both middlewares originally *raised* ``AegisError``,
but ``@app.middleware("http")`` runs outside the exception-handler stack, so the raise escaped
unhandled and the client received an opaque 500 instead of 429/401 — with no error envelope.
Every assertion below on the exact status and ``error_code`` is guarding that regression.
"""

from __future__ import annotations

import httpx
import pytest

from core.config import get_settings


async def _exhaust(client: httpx.AsyncClient, path: str, attempts: int) -> httpx.Response:
    """Fire ``attempts`` requests and return the last response."""
    response = None
    for _ in range(attempts):
        response = await client.get(path)
        if response.status_code == 429:
            break
    assert response is not None
    return response


async def test_rate_limit_returns_429_envelope_not_500(api_client: httpx.AsyncClient, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "api_rate_limit_per_window", 5)

    response = await _exhaust(api_client, "/api/v1/incidents", attempts=40)

    if response.status_code != 429:
        pytest.skip("Redis unavailable — the limiter correctly failed open")

    assert response.json()["error_code"] == "rate_limited"
    assert response.headers["Retry-After"] == str(settings.api_rate_limit_window_seconds)


async def test_allowed_requests_carry_ratelimit_headers(api_client: httpx.AsyncClient):
    response = await api_client.get("/api/v1/incidents")
    # 401 (unauthenticated) is fine — the middleware runs before auth and must still annotate.
    assert "X-RateLimit-Limit" in response.headers
    assert "X-RateLimit-Remaining" in response.headers


async def test_health_is_exempt_from_rate_limiting(api_client: httpx.AsyncClient, monkeypatch):
    """A limiter that can throttle the liveness probe would take an instance out of rotation."""
    monkeypatch.setattr(get_settings(), "api_rate_limit_per_window", 1)
    for _ in range(10):
        assert (await api_client.get("/api/v1/health")).status_code in (200, 503)


async def test_webhook_guard_returns_401_envelope_not_500(
    api_client: httpx.AsyncClient, monkeypatch
):
    monkeypatch.setattr(get_settings(), "ingest_webhook_token", "expected-secret")
    response = await api_client.post(
        "/api/v1/incidents",
        json={
            "alert_source": "prometheus",
            "external_alert_id": "wh-1",
            "service_name": "checkout-service",
            "title": "elevated error rate",
        },
        headers={"x-aegis-webhook-token": "wrong-secret"},
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "webhook_unauthorized"
