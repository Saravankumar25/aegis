"""Slack MCP server — the Communication Agent's outbound channel (FR-6, ESD §3).

Run standalone:  python -m mcp_servers.slack.server
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from core.logging import configure_logging
from mcp_servers.common import ToolResult, guarded
from mcp_servers.slack.client import SOURCE, SlackClient

app = FastMCP("aegis-slack")

_client: SlackClient | None = None


def _get_client() -> SlackClient:
    global _client
    if _client is None:
        _client = SlackClient()
    return _client


@app.tool()
async def post_message(text: str) -> dict[str, Any]:
    """Post one plain-English status update. Unconfigured webhook = clean non-delivery."""

    async def fetch() -> ToolResult:
        result, attempts = await _get_client().post_message(text)
        return ToolResult(
            ok=True, source=SOURCE, tool="post_message", data=result, attempts=attempts
        )

    return (await guarded(SOURCE, "post_message", fetch)).model_dump()


if __name__ == "__main__":
    configure_logging()
    app.run()
