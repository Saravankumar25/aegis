"""Slack webhook client for the Communication Agent (FR-6).

Uses an incoming-webhook URL from env (per-server credential, ESD §16). When no webhook is
configured the client reports a clean non-delivery instead of failing — Slack is an output
channel, never a dependency the investigation can stall on.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.config import get_settings
from mcp_servers.common import retry_transient

SOURCE = "slack"


class SlackClient:
    """Thin adapter over a Slack incoming webhook (Adapter pattern, ESD §24)."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._webhook_url = settings.slack_webhook_url
        self._attempts = settings.mcp_retry_attempts
        self._base_delay = settings.mcp_retry_base_delay_seconds
        self._http = http or httpx.AsyncClient(timeout=settings.mcp_http_timeout_seconds)

    async def post_message(self, text: str) -> tuple[dict[str, Any], int]:
        if not self._webhook_url:
            return {"delivered": False, "reason": "slack webhook not configured"}, 1
        _response, attempts = await retry_transient(
            lambda: self._http.post(self._webhook_url, json={"text": text}),
            attempts=self._attempts,
            base_delay_seconds=self._base_delay,
            source=SOURCE,
            tool="post_message",
        )
        return {"delivered": True}, attempts

    async def aclose(self) -> None:
        await self._http.aclose()
