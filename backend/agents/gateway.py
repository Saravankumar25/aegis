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

import json
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from core.logging import get_logger

BACKEND_DIR = Path(__file__).resolve().parents[1]

ToolResultDict = dict[str, Any]


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
        try:
            result = await session.call_tool(tool, arguments or {})
            text = "".join(c.text for c in result.content if getattr(c, "text", None))
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001 — degrade, never stall the run (ESD §12)
            self._log.warning("mcp_call_failed", server=server, tool=tool, error=str(exc))
            return _unavailable(server, tool, str(exc))
