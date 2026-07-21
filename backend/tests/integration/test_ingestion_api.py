"""Integration tests: ingestion idempotency (FR-10.1, the M1 promise) + dedup (FR-1.2).

The `api_client` fixture sends a valid webhook token on every request, so each test below
also exercises the ingest guard rather than depending on it being switched off. The
rejection cases at the bottom are the other half of that: an ingestion endpoint that
accepts anonymous alerts lets anyone create incidents and drive the agent pipeline, which
costs tokens and pages humans.
"""

from __future__ import annotations

import httpx


def _alert(**overrides) -> dict:
    base = {
        "alert_source": "prometheus",
        "external_alert_id": "alert-001",
        "service_name": "checkout-service",
        "title": "High 5xx rate on checkout-service",
        "kind": "error_rate",
        "value": 0.42,
    }
    return {**base, **overrides}


async def test_ingest_creates_incident_with_classified_severity(api_client: httpx.AsyncClient):
    response = await api_client.post("/api/v1/incidents", json=_alert())
    assert response.status_code == 201
    body = response.json()
    assert body["created"] is True
    assert body["severity"] == "P1"  # critical service × error_rate (FR-1.3)
    assert body["state"] == "open"


async def test_ingest_is_idempotent_on_external_id(api_client: httpx.AsyncClient):
    first = (await api_client.post("/api/v1/incidents", json=_alert())).json()
    second = (await api_client.post("/api/v1/incidents", json=_alert())).json()
    assert second["created"] is False
    assert second["deduplicated"] is False
    assert second["incident_id"] == first["incident_id"]


async def test_ingest_dedups_same_service_within_window(api_client: httpx.AsyncClient):
    first = (await api_client.post("/api/v1/incidents", json=_alert())).json()
    # Different external id, same service+source, inside the 5-minute window → merged.
    second = (
        await api_client.post(
            "/api/v1/incidents",
            json=_alert(external_alert_id="alert-002", title="p99 latency spike"),
        )
    ).json()
    assert second["deduplicated"] is True
    assert second["incident_id"] == first["incident_id"]


async def test_different_service_is_not_deduplicated(api_client: httpx.AsyncClient):
    first = (await api_client.post("/api/v1/incidents", json=_alert())).json()
    second = (
        await api_client.post(
            "/api/v1/incidents",
            json=_alert(external_alert_id="alert-003", service_name="payment-service"),
        )
    ).json()
    assert second["created"] is True
    assert second["incident_id"] != first["incident_id"]


async def test_error_envelope_shape_on_validation_failure(api_client: httpx.AsyncClient):
    response = await api_client.post("/api/v1/incidents", json={"nope": True})
    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"error_code", "message", "incident_id"}
    assert body["error_code"] == "validation_error"


# --- the ingest guard itself (previously untested) ----------------------------------------


async def test_ingest_rejects_a_missing_webhook_token(api_client: httpx.AsyncClient):
    response = await api_client.post(
        "/api/v1/incidents", json=_alert(), headers={"x-aegis-webhook-token": ""}
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "webhook_unauthorized"


async def test_ingest_rejects_a_wrong_webhook_token(api_client: httpx.AsyncClient):
    response = await api_client.post(
        "/api/v1/incidents", json=_alert(), headers={"x-aegis-webhook-token": "not-the-token"}
    )
    assert response.status_code == 401


async def test_rejected_ingest_creates_no_incident(api_client: httpx.AsyncClient):
    """A 401 must happen before any write, or the guard is only cosmetic."""
    await api_client.post(
        "/api/v1/incidents",
        json=_alert(external_alert_id="alert-unauthorized"),
        headers={"x-aegis-webhook-token": "not-the-token"},
    )
    # The same external id now succeeds and reports `created`, proving the rejected
    # request left nothing behind.
    accepted = await api_client.post(
        "/api/v1/incidents", json=_alert(external_alert_id="alert-unauthorized")
    )
    assert accepted.status_code == 201
    assert accepted.json()["created"] is True
