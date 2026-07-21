"""PagerDuty-mock MCP server: fixture-replay engine (ESD §3 Part B).

Replays canned PagerDuty-style incidents from ``eval/pagerduty_fixtures/`` so demos and
evals can exercise a second alert source with zero external dependency.
Run standalone:  python -m mcp_servers.pagerduty_mock.server
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from core.logging import configure_logging
from mcp_servers.common import ToolResult, UpstreamRequestError, guarded

SOURCE = "pagerduty_mock"
FIXTURE_DIR = Path(__file__).resolve().parents[3] / "eval" / "pagerduty_fixtures"

app = FastMCP("aegis-pagerduty-mock")


def _load_all() -> list[dict[str, Any]]:
    if not FIXTURE_DIR.exists():
        return []
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURE_DIR.glob("*.json"))]


@app.tool()
async def list_incidents() -> dict[str, Any]:
    """List all fixture incidents (the replay corpus)."""

    async def fetch() -> ToolResult:
        return ToolResult(
            ok=True,
            source=SOURCE,
            tool="list_incidents",
            contains_untrusted_text=True,  # fixture titles/notes are still free text
            data=_load_all(),
        )

    return (await guarded(SOURCE, "list_incidents", fetch)).model_dump()


@app.tool()
async def get_incident(incident_id: str) -> dict[str, Any]:
    """Fetch one fixture incident by its `id` field."""

    async def fetch() -> ToolResult:
        for item in _load_all():
            if item.get("id") == incident_id:
                return ToolResult(
                    ok=True,
                    source=SOURCE,
                    tool="get_incident",
                    contains_untrusted_text=True,
                    data=item,
                )
        raise UpstreamRequestError(404, f"no fixture incident '{incident_id}'")

    return (await guarded(SOURCE, "get_incident", fetch)).model_dump()


if __name__ == "__main__":
    configure_logging()
    app.run()
