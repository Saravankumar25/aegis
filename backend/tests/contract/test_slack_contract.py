"""Contract tests: the Slack MCP server (ESD §22).

The PagerDuty-mock server was removed: it served fabricated incidents, and real
alerts already arrive from Prometheus/Alertmanager. Aegis ingests real alert data
only.
"""

from __future__ import annotations

import httpx

from mcp_servers.slack.client import SlackClient


async def test_slack_unconfigured_reports_clean_non_delivery(monkeypatch):
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "slack_webhook_url", None)
    result, attempts = await SlackClient().post_message("hello")
    assert result == {"delivered": False, "reason": "slack webhook not configured"}


async def test_slack_posts_to_webhook(monkeypatch, mock_client):
    from core.config import get_settings

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(get_settings(), "slack_webhook_url", "http://fixture.test/webhook")
    client = SlackClient(http=mock_client(handler))
    result, _ = await client.post_message("[Aegis] update text")
    assert result["delivered"] is True
    assert sent == [{"text": "[Aegis] update text"}]
