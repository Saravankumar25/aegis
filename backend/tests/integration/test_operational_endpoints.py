"""Integration tests: operational endpoint surface (ESD §7, §16).

Three things that only matter on the first real deployment, and would each have been found
in production rather than in CI:

* `/health` served a 404 while the endpoint lived under `/api/v1/health`. Kubernetes probes
  and load balancers use the unversioned path, and ESD §7 documents it — so the first
  deployment would have read every instance as dead.
* `/metrics` is authenticated at both paths. That is a deliberate deviation from the
  convention that `/metrics` is anonymous: incident counts and Redis keyspace stats describe
  production activity, so a scraper is given credentials rather than the endpoint opened.
* `/docs`, `/redoc` and `/openapi.json` enumerate every route, schema and auth requirement.
  Served unauthenticated, that is a complete map of the attack surface.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.anyio


# --- health: reachable where infrastructure actually probes -------------------------------


@pytest.mark.parametrize("path", ["/health", "/api/v1/health"])
async def test_health_is_served_at_both_paths(api_client: httpx.AsyncClient, path: str):
    response = await api_client.get(path)
    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded"}


@pytest.mark.parametrize("path", ["/health", "/api/v1/health"])
async def test_health_needs_no_authentication(api_client: httpx.AsyncClient, path: str):
    """A probe cannot hold a session cookie; an authenticated health check reports every
    instance unhealthy. The fixture client carries no session, so this is the probe's view."""
    response = await api_client.get(path)
    assert response.status_code != 401


async def test_health_reports_each_dependency_separately(api_client: httpx.AsyncClient):
    """Postgres and Redis differ in role, and the status must reflect that: losing the cache
    must not take an instance out of rotation."""
    body = (await api_client.get("/health")).json()
    assert set(body["checks"]) == {"postgres", "redis"}


# --- metrics: authenticated at both paths --------------------------------------------------


@pytest.mark.parametrize("path", ["/metrics", "/api/v1/metrics"])
async def test_metrics_requires_authentication(api_client: httpx.AsyncClient, path: str):
    response = await api_client.get(path)
    assert response.status_code == 401, (
        "metrics expose incident counts and Redis keyspace stats; the scraper gets "
        "credentials rather than the endpoint being opened"
    )


# --- docs: off outside development ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_docs_are_disabled_in_production(path: str, monkeypatch, db_url):
    """Switched by environment rather than a flag someone has to remember to set."""
    import os

    from core.config import get_settings

    os.environ["ENVIRONMENT"] = "production"
    os.environ["DATABASE_URL"] = db_url
    get_settings.cache_clear()
    try:
        from api.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert (await client.get(path)).status_code == 404
    finally:
        os.environ["ENVIRONMENT"] = "test"
        get_settings.cache_clear()


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
async def test_docs_remain_available_outside_production(api_client: httpx.AsyncClient, path: str):
    """They are genuinely useful while developing; the control is environmental, not a ban."""
    assert (await api_client.get(path)).status_code == 200
