"""Contract tests: Prometheus MCP server against fixture responses (ESD §22)."""

from __future__ import annotations

import httpx
import pytest

from mcp_servers.common import ErrorKind, UpstreamRequestError, guarded
from mcp_servers.prometheus.client import SOURCE, PrometheusClient


def _handler_for(fixtures: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = fixtures.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"status": "error", "error": "unknown endpoint"})
        return httpx.Response(200, json=body)

    return handler


async def test_query_metrics_parses_vector(load_fixture, mock_client):
    client = PrometheusClient(
        http=mock_client(
            _handler_for({"/api/v1/query": load_fixture("prometheus/query_instant.json")})
        )
    )
    result, attempts = await client.query_metrics('http_requests_total{service="checkout-service"}')
    assert attempts == 1
    assert result.result_type == "vector"
    assert len(result.samples) == 2
    errors = next(s for s in result.samples if s.metric["status"] == "500")
    assert errors.value == "670"
    assert errors.timestamp == pytest.approx(1784023200.0)


async def test_query_range_metrics_parses_matrix(load_fixture, mock_client):
    client = PrometheusClient(
        http=mock_client(
            _handler_for({"/api/v1/query_range": load_fixture("prometheus/query_range.json")})
        )
    )
    result, _ = await client.query_range_metrics(
        "rate(http_requests_total[1m])", start="1784023140", end="1784023200", step="30s"
    )
    (series,) = result.series
    assert series.metric["status"] == "500"
    assert series.values[-1] == (1784023200.0, "670")


async def test_list_alerts_parses_firing_alert(load_fixture, mock_client):
    client = PrometheusClient(
        http=mock_client(_handler_for({"/api/v1/alerts": load_fixture("prometheus/alerts.json")}))
    )
    alerts, _ = await client.list_alerts()
    (alert,) = alerts
    assert alert.name == "HighErrorRate" and alert.state == "firing"
    assert alert.labels["service"] == "checkout-service"


async def test_in_body_query_error_becomes_bad_request_envelope(mock_client):
    """Prometheus reports bad PromQL in-body (HTTP 200) — must map to bad_request, no retry."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"status": "error", "errorType": "bad_data", "error": "parse error"}
        )

    client = PrometheusClient(http=mock_client(handler))

    with pytest.raises(UpstreamRequestError):
        await client.query_metrics("this{is=not promql")
    assert calls == 1  # non-transient: exactly one attempt

    # And through the guarded envelope it degrades, not raises.
    async def fetch_envelope():
        await client.query_metrics("this{is=not promql")
        raise AssertionError("unreachable")  # pragma: no cover

    envelope = await guarded(SOURCE, "query_metrics", fetch_envelope)
    assert envelope.ok is False
    assert envelope.error_kind == ErrorKind.bad_request
