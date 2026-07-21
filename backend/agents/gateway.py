"""Evidence gateway: how agents reach the MCP servers (ESD §10, §16).

``McpGateway`` speaks the real MCP protocol over stdio to the three read-only servers,
each running as its own subprocess with its own credential — the worker process never
holds an infrastructure credential itself (PRD NFR-Security). There is no in-app
fake or offline gateway: evidence either comes from real infrastructure or is
recorded as a documented gap. Tests use ``tests/support/doubles.ReplayGateway``.

Every method returns the servers' uniform ``ToolResult`` dict; a dead server yields
``ok=false`` envelopes (their own retry/degradation logic, ESD §12) or, if the subprocess
itself is gone, a synthesized unavailable envelope — the investigation continues with a
documented gap either way.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core.config import get_settings
from core.logging import get_logger
from core.redis import cache_get, cache_set
from core.tracing import annotate, trace_span

BACKEND_DIR = Path(__file__).resolve().parents[1]

ToolResultDict = dict[str, Any]


# Cacheable tools, named explicitly. This is an allowlist rather than a denylist of writes
# on purpose: with a denylist, a future write tool is cacheable until someone remembers to
# exclude it, and the failure mode is returning a cached "success" for an action that was
# never executed against the cluster. Anything not named here goes straight to the server.
_CACHEABLE_READS: frozenset[tuple[str, str]] = frozenset(
    {
        ("k8s", "list_pods"),
        ("k8s", "get_pod"),
        ("k8s", "get_pod_logs"),
        ("k8s", "list_events"),
        ("k8s", "list_deployments"),
        ("prometheus", "query_metrics"),
        ("prometheus", "query_range_metrics"),
        ("prometheus", "list_alerts"),
        ("github", "get_recent_commits"),
        ("github", "get_commit_diff"),
        ("github", "list_pull_requests"),
    }
)


def _evidence_cache_key(server: str, tool: str, arguments: dict | None) -> str | None:
    """Stable cache key for a read-only tool call, or None if it must not be cached."""
    if (server, tool) not in _CACHEABLE_READS:
        return None
    # sort_keys makes the key independent of argument ordering, so two callers asking the
    # same question share an entry instead of each paying for their own round trip.
    fingerprint = json.dumps(arguments or {}, sort_keys=True, default=str)
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()[:32]
    return f"mcp:{server}:{tool}:{digest}"


def _unavailable(source: str, tool: str, reason: str) -> ToolResultDict:
    return {
        "ok": False,
        "source": source,
        "tool": tool,
        "error_kind": "unavailable",
        "error": reason,
        "contains_untrusted_text": False,
        "data": None,
        "attempts": 0,
    }


class McpGateway:
    """Real MCP stdio client sessions to the k8s / prometheus / github servers."""

    SERVERS = ("k8s", "prometheus", "github", "slack")

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._sessions: dict[str, ClientSession] = {}
        self._log = get_logger(component="mcp_gateway")

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        for name in self.SERVERS:
            try:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", f"mcp_servers.{name}.server"],
                    cwd=str(BACKEND_DIR),
                    env=dict(os.environ),
                )
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self._sessions[name] = session
                self._log.info("mcp_server_connected", server=name)
            except Exception as exc:  # noqa: BLE001 — a dead server is a documented gap
                self._log.warning("mcp_server_unavailable", server=name, error=str(exc))

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._sessions.clear()

    async def call(self, server: str, tool: str, arguments: dict | None = None) -> ToolResultDict:
        session = self._sessions.get(server)
        if session is None:
            return _unavailable(server, tool, "MCP server process not running")

        cache_key = _evidence_cache_key(server, tool, arguments)
        if cache_key is not None:
            cached = await cache_get(cache_key)
            if cached is not None:
                self._log.debug("mcp_cache_hit", server=server, tool=tool)
                return cached

        with trace_span(f"tool:{server}.{tool}", run_type="tool", arguments=arguments or {}):
            try:
                result = await session.call_tool(tool, arguments or {})
                text = "".join(c.text for c in result.content if getattr(c, "text", None))
                payload: ToolResultDict = json.loads(text)
            except Exception as exc:  # noqa: BLE001 — degrade, never stall the run (ESD §12)
                self._log.warning("mcp_call_failed", server=server, tool=tool, error=str(exc))
                return _unavailable(server, tool, str(exc))
            annotate(ok=payload.get("ok"), error_kind=payload.get("error_kind"))

        # Only successful reads are cached. Caching a failure would turn one transient blip
        # into a whole TTL of manufactured unavailability, and the ensemble's later passes
        # are exactly when a recovered source should be picked up again.
        if cache_key is not None and payload.get("ok"):
            await cache_set(
                cache_key, payload, ttl_seconds=get_settings().evidence_cache_ttl_seconds
            )
        return payload
