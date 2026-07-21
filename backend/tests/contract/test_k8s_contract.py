"""Contract tests: k8s MCP server against fixture responses (CLAUDE.md §7, ESD §22)."""

from __future__ import annotations

import json

import httpx

from mcp_servers.k8s.client import K8sClient


def _handler_for(fixtures: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path in fixtures:
            body = fixtures[path]
            if isinstance(body, str):  # pod logs endpoint returns plain text
                return httpx.Response(200, text=body)
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"kind": "Status", "message": f"not found: {path}"})

    return handler


async def test_list_pods_parses_summary(load_fixture, mock_client):
    client = K8sClient(
        http=mock_client(
            _handler_for({"/api/v1/namespaces/meridian/pods": load_fixture("k8s/pod_list.json")})
        )
    )
    pods, attempts = await client.list_pods("meridian")
    assert attempts == 1
    assert [p.name for p in pods] == [
        "checkout-service-6d5f8b9c7d-x2k4p",
        "payment-service-7c9d4f5b6a-q8w3e",
    ]
    healthy, crashing = pods
    assert healthy.ready == "1/1" and healthy.restarts == 0
    assert crashing.ready == "0/1" and crashing.restarts == 7
    assert crashing.phase == "Running"  # phase alone hides CrashLoopBackOff; detail has it


async def test_get_pod_surfaces_crashloop_and_oomkill(load_fixture, mock_client):
    path = "/api/v1/namespaces/meridian/pods/payment-service-7c9d4f5b6a-q8w3e"
    client = K8sClient(http=mock_client(_handler_for({path: load_fixture("k8s/pod_get.json")})))
    pod, _ = await client.get_pod("payment-service-7c9d4f5b6a-q8w3e", "meridian")
    assert pod.conditions["Ready"] == "False"
    (container,) = pod.containers
    assert container.state == "waiting"
    assert container.state_reason == "CrashLoopBackOff"
    assert container.last_terminated_reason == "OOMKilled"


async def test_get_pod_logs_returns_text_with_bounds(mock_client):
    log_text = "ERROR payment timeout\nERROR payment timeout\n"
    path = "/api/v1/namespaces/meridian/pods/payment-service-7c9d4f5b6a-q8w3e/log"
    client = K8sClient(http=mock_client(_handler_for({path: log_text})))
    logs, _ = await client.get_pod_logs(
        "payment-service-7c9d4f5b6a-q8w3e", "meridian", tail_lines=50
    )
    assert logs.text == log_text
    assert logs.tail_lines == 50


async def test_list_events_parses_warning(load_fixture, mock_client):
    client = K8sClient(
        http=mock_client(
            _handler_for(
                {"/api/v1/namespaces/meridian/events": load_fixture("k8s/event_list.json")}
            )
        )
    )
    events, _ = await client.list_events("meridian")
    warning = events[0]
    assert warning.type == "Warning" and warning.reason == "BackOff"
    assert warning.involved_object == "Pod/payment-service-7c9d4f5b6a-q8w3e"
    assert warning.count == 12


async def test_list_deployments_parses_replica_health(load_fixture, mock_client):
    client = K8sClient(
        http=mock_client(
            _handler_for(
                {
                    "/apis/apps/v1/namespaces/meridian/deployments": load_fixture(
                        "k8s/deployment_list.json"
                    )
                }
            )
        )
    )
    deployments, _ = await client.list_deployments("meridian")
    degraded = next(d for d in deployments if d.name == "payment-service")
    assert degraded.replicas_desired == 2 and degraded.replicas_ready == 1
    assert degraded.images == ["meridian/simulator:latest"]


async def test_tool_envelope_marks_logs_untrusted(mock_client):
    """The MCP tool layer must flag free-text evidence for the redaction boundary."""
    from mcp_servers.k8s import server

    path = "/api/v1/namespaces/meridian/pods/some-pod/log"
    server._client = K8sClient(http=mock_client(_handler_for({path: "line1\nline2\n"})))
    try:
        envelope = await server.get_pod_logs("some-pod", namespace="meridian")
    finally:
        server._client = None
    assert envelope["ok"] is True
    assert envelope["contains_untrusted_text"] is True
    assert envelope["source"] == "k8s" and envelope["tool"] == "get_pod_logs"
    # The envelope must survive a JSON round-trip (it crosses the MCP boundary as JSON).
    assert json.loads(json.dumps(envelope))["data"]["text"] == "line1\nline2\n"
