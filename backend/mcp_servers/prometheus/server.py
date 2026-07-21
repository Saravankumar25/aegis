"""Prometheus MCP server — metric and alert evidence tools for the investigation agents.

Exposes `verb_noun` tools (CLAUDE.md §4) over MCP stdio, returning the uniform
``ToolResult`` envelope with graceful degradation (ESD §12).
Run standalone:  python -m mcp_servers.prometheus.server
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from core.config import get_settings
from core.logging import configure_logging
from mcp_servers.common import ToolResult, guarded
from mcp_servers.prometheus.client import SOURCE, PrometheusClient

app = FastMCP("aegis-prometheus")

_client: PrometheusClient | None = None


def _get_client() -> PrometheusClient:
    global _client
    if _client is None:
        _client = PrometheusClient()
    return _client


@app.tool()
async def query_metrics(query: str, time: str | None = None) -> dict[str, Any]:
    """Run an instant PromQL query (optionally at an RFC3339/unix `time`)."""

    async def fetch() -> ToolResult:
        result, attempts = await _get_client().query_metrics(query, time=time)
        return ToolResult(
            ok=True,
            source=SOURCE,
            tool="query_metrics",
            data=result.model_dump(),
            attempts=attempts,
        )

    return (await guarded(SOURCE, "query_metrics", fetch)).model_dump()


@app.tool()
async def query_range_metrics(
    query: str, start: str, end: str, step: str = "30s"
) -> dict[str, Any]:
    """Run a PromQL range query between `start` and `end` (RFC3339 or unix seconds)."""

    async def fetch() -> ToolResult:
        result, attempts = await _get_client().query_range_metrics(query, start, end, step=step)
        return ToolResult(
            ok=True,
            source=SOURCE,
            tool="query_range_metrics",
            data=result.model_dump(),
            attempts=attempts,
        )

    return (await guarded(SOURCE, "query_range_metrics", fetch)).model_dump()


@app.tool()
async def list_alerts(namespace: str | None = None, all_namespaces: bool = False) -> dict[str, Any]:
    """List currently active (firing/pending) Prometheus alerts.

    Scoped to the application namespace by default. The safe scope is the default and the
    broad one is explicit, because the caller that omits the argument is a model choosing
    tools: an unscoped default silently fed `etcdMembersDown` from `kube-system` into a
    `checkout-service` investigation, and RCA twice built its hypothesis around a
    control-plane failure unrelated to the incident.

    Set ``all_namespaces`` for a genuine cluster-wide investigation.
    """
    # `k8s_namespace` is the namespace the monitored application runs in; it is the same
    # scope whether it is reached through the k8s API or through Prometheus labels.
    scope = None if all_namespaces else (namespace or get_settings().k8s_namespace)

    async def fetch() -> ToolResult:
        alerts, attempts = await _get_client().list_alerts(namespace=scope)
        return ToolResult(
            ok=True,
            source=SOURCE,
            tool="list_alerts",
            contains_untrusted_text=True,  # operator-authored annotations/labels
            data=[a.model_dump() for a in alerts],
            attempts=attempts,
        )

    return (await guarded(SOURCE, "list_alerts", fetch)).model_dump()


if __name__ == "__main__":
    configure_logging()
    app.run()
