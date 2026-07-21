"""GitHub MCP server — deploy/change evidence tools for the investigation agents (FR-2.2).

Exposes `verb_noun` tools (CLAUDE.md §4) over MCP stdio, returning the uniform
``ToolResult`` envelope with graceful degradation (ESD §12). Commit messages, PR titles,
and patches are untrusted free text and flagged as such.
Run standalone:  python -m mcp_servers.github.server
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from core.logging import configure_logging
from mcp_servers.common import ToolResult, guarded
from mcp_servers.github.client import SOURCE, GitHubClient

app = FastMCP("aegis-github")

_client: GitHubClient | None = None


def _get_client() -> GitHubClient:
    global _client
    if _client is None:
        _client = GitHubClient()
    return _client


@app.tool()
async def get_recent_commits(
    lookback_hours: float = 2.0, branch: str | None = None, limit: int = 30
) -> dict[str, Any]:
    """List commits in the lookback window (default 2h, FR-2.2). Messages are untrusted."""

    async def fetch() -> ToolResult:
        commits, attempts = await _get_client().get_recent_commits(
            lookback_hours=lookback_hours, branch=branch, limit=limit
        )
        return ToolResult(
            ok=True,
            source=SOURCE,
            tool="get_recent_commits",
            contains_untrusted_text=True,
            data=[c.model_dump() for c in commits],
            attempts=attempts,
        )

    return (await guarded(SOURCE, "get_recent_commits", fetch)).model_dump()


@app.tool()
async def list_pull_requests(state: str = "closed", limit: int = 20) -> dict[str, Any]:
    """List recently updated pull requests. Titles are untrusted free text."""

    async def fetch() -> ToolResult:
        pulls, attempts = await _get_client().list_pull_requests(state=state, limit=limit)
        return ToolResult(
            ok=True,
            source=SOURCE,
            tool="list_pull_requests",
            contains_untrusted_text=True,
            data=[p.model_dump() for p in pulls],
            attempts=attempts,
        )

    return (await guarded(SOURCE, "list_pull_requests", fetch)).model_dump()


@app.tool()
async def get_commit_diff(sha: str) -> dict[str, Any]:
    """Fetch one commit's changed files and (bounded) patches. Patches are untrusted."""

    async def fetch() -> ToolResult:
        diff, attempts = await _get_client().get_commit_diff(sha)
        return ToolResult(
            ok=True,
            source=SOURCE,
            tool="get_commit_diff",
            contains_untrusted_text=True,
            data=diff.model_dump(),
            attempts=attempts,
        )

    return (await guarded(SOURCE, "get_commit_diff", fetch)).model_dump()


if __name__ == "__main__":
    configure_logging()
    app.run()
