"""Fault-injection tests for the MCP servers' graceful degradation (ESD §12, §22).

Each scenario feeds a broken upstream to a real client and asserts the documented
behavior: transient failures retry with backoff then degrade to a structured
``ToolResult{ok=false, error_kind="unavailable"}``; malformed data fails fast as
``malformed_response``; and a failure never poisons the server — the next good call
succeeds. No scenario may crash or leak an exception across the tool boundary.
"""

from __future__ import annotations

import httpx
import pytest

from mcp_servers.common import (
    ErrorKind,
    MalformedResponseError,
    SourceUnavailableError,
    ToolResult,
    guarded,
    retry_transient,
)
from mcp_servers.github.client import GitHubClient
from mcp_servers.k8s.client import K8sClient
from mcp_servers.prometheus.client import PrometheusClient


def _fast(client: K8sClient | PrometheusClient | GitHubClient) -> object:
    """Zero out backoff delay so fault tests don't sleep."""
    client._base_delay = 0.0
    return client


def _mock(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://fault.test", transport=httpx.MockTransport(handler))


# --- upstream killed mid-call (connection refused / dropped) ---------------------------------


async def test_k8s_connection_error_degrades_to_unavailable_after_retries():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    client = _fast(K8sClient(http=_mock(handler)))

    async def fetch() -> ToolResult:
        pods, attempts = await client.list_pods("meridian")
        raise AssertionError("unreachable")  # pragma: no cover

    envelope = await guarded("k8s", "list_pods", fetch)
    assert envelope.ok is False
    assert envelope.error_kind == ErrorKind.unavailable
    assert calls == 3  # ESD §12: exactly 3 attempts, then mark unavailable


async def test_prometheus_timeout_degrades_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    client = _fast(PrometheusClient(http=_mock(handler)))
    with pytest.raises(SourceUnavailableError):
        await client.query_metrics("up")


async def test_github_repeated_500_retries_then_degrades():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, text="Internal Server Error")

    client = _fast(GitHubClient(http=_mock(handler)))

    async def fetch() -> ToolResult:
        await client.get_recent_commits()
        raise AssertionError("unreachable")  # pragma: no cover

    envelope = await guarded("github", "get_recent_commits", fetch)
    assert envelope.ok is False and envelope.error_kind == ErrorKind.unavailable
    assert calls == 3


# --- malformed data (fail fast, no retry) ----------------------------------------------------


async def test_malformed_json_fails_fast_as_malformed_response():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text="<html>not json at all</html>")

    client = _fast(PrometheusClient(http=_mock(handler)))

    async def fetch() -> ToolResult:
        await client.list_alerts()
        raise AssertionError("unreachable")  # pragma: no cover

    envelope = await guarded("prometheus", "list_alerts", fetch)
    assert envelope.ok is False
    assert envelope.error_kind == ErrorKind.malformed_response
    assert calls == 1  # parse errors are not transient; retrying wastes the time budget


async def test_wrong_shape_json_is_malformed_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{"unexpected": "shape"}]})

    client = _fast(K8sClient(http=_mock(handler)))
    with pytest.raises(MalformedResponseError):
        await client.list_pods("meridian")


# --- 4xx classification ----------------------------------------------------------------------


async def test_404_maps_to_not_found_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"kind": "Status", "message": "pod not found"})

    client = _fast(K8sClient(http=_mock(handler)))

    async def fetch() -> ToolResult:
        await client.get_pod("ghost-pod", "meridian")
        raise AssertionError("unreachable")  # pragma: no cover

    envelope = await guarded("k8s", "get_pod", fetch)
    assert envelope.ok is False and envelope.error_kind == ErrorKind.not_found


# --- recovery: a failure must not poison the server ------------------------------------------


async def test_server_recovers_after_upstream_comes_back():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:  # first tool call: upstream fully down
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"resultType": "vector", "result": []},
            },
        )

    client = _fast(PrometheusClient(http=_mock(handler)))

    async def fetch() -> ToolResult:
        result, attempts = await client.query_metrics("up")
        return ToolResult(
            ok=True,
            source="prometheus",
            tool="query_metrics",
            data=result.model_dump(),
            attempts=attempts,
        )

    first = await guarded("prometheus", "query_metrics", fetch)
    assert first.ok is False and first.error_kind == ErrorKind.unavailable

    second = await guarded("prometheus", "query_metrics", fetch)
    assert second.ok is True
    assert second.data["result_type"] == "vector"


# --- backoff shape (deterministic logic unit-checked here, ESD §12) ---------------------------


async def test_retry_backoff_is_exponential(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("mcp_servers.common.asyncio.sleep", fake_sleep)

    attempts = 0

    async def call() -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("down", request=httpx.Request("GET", "http://fault.test/x"))

    with pytest.raises(SourceUnavailableError):
        await retry_transient(call, attempts=3, base_delay_seconds=0.2, source="t", tool="t")
    assert attempts == 3
    assert sleeps == [0.2, 0.4]  # base, base*2 — no sleep after the final failure
