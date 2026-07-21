"""Contract tests: Slack + PagerDuty-mock MCP servers (ESD §22)."""

from __future__ import annotations

import httpx

from mcp_servers.pagerduty_mock import server as pd_server
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


async def test_pagerduty_mock_replays_fixtures():
    envelope = await pd_server.list_incidents()
    assert envelope["ok"] is True
    assert envelope["contains_untrusted_text"] is True
    ids = {item["id"] for item in envelope["data"]}
    assert {"PD-1001", "PD-1002"} <= ids

    one = await pd_server.get_incident("PD-1001")
    assert one["ok"] is True
    assert one["data"]["service_name"] == "checkout-service"

    missing = await pd_server.get_incident("PD-9999")
    assert missing["ok"] is False
    assert missing["error_kind"] == "not_found"
