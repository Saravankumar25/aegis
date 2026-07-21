"""Prometheus MCP server — metric and alert evidence tools for the investigation agents.

Exposes `verb_noun` tools (CLAUDE.md §4) over MCP stdio, returning the uniform
``ToolResult`` envelope with graceful degradation (ESD §12).
Run standalone:  python -m mcp_servers.prometheus.server
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

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
async def list_alerts() -> dict[str, Any]:
    """List currently active (firing/pending) Prometheus alerts."""

    async def fetch() -> ToolResult:
        alerts, attempts = await _get_client().list_alerts()
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
