"""Contract tests: GitHub MCP server against fixture responses (ESD §22, FR-2.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx

from mcp_servers.github.client import MAX_PATCH_CHARS, GitHubClient


async def test_get_recent_commits_uses_lookback_window(load_fixture, mock_client):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k: v[0] for k, v in parse_qs(request.url.query.decode()).items()})
        return httpx.Response(200, json=load_fixture("github/commit_list.json"))

    client = GitHubClient(http=mock_client(handler))
    commits, attempts = await client.get_recent_commits(lookback_hours=2.0, branch="main")

    assert attempts == 1
    assert [c.sha[:7] for c in commits] == ["9f1c2e3", "1a2b3c4"]
    assert commits[0].message.startswith("feat: raise checkout cache TTL")
    assert seen["sha"] == "main"
    # `since` must be ~2 hours ago (FR-2.2 default lookback window).
    since = datetime.fromisoformat(seen["since"])
    assert abs((datetime.now(UTC) - timedelta(hours=2)) - since) < timedelta(minutes=5)


async def test_list_pull_requests_parses_summary(load_fixture, mock_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=load_fixture("github/pull_list.json"))

    client = GitHubClient(http=mock_client(handler))
    pulls, _ = await client.list_pull_requests()
    (pr,) = pulls
    assert pr.number == 42 and pr.state == "closed"
    assert pr.author == "Saravankumar25"
    assert pr.head_branch == "feat/cache-ttl"


async def test_get_commit_diff_parses_files_and_caps_patch(load_fixture, mock_client):
    fixture = load_fixture("github/commit_diff.json")
    # Inflate one patch beyond the cap to prove truncation (token-budget guard, ESD §15).
    fixture["files"][0]["patch"] = "x" * (MAX_PATCH_CHARS + 500)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=fixture)

    client = GitHubClient(http=mock_client(handler))
    diff, _ = await client.get_commit_diff("9f1c2e3d4b5a69788c7d6e5f4a3b2c1d0e9f8a7b")
    assert diff.total_additions == 3 and diff.total_deletions == 1
    (file,) = diff.files
    assert file.filename == "services/checkout/config.py"
    assert file.patch is not None and len(file.patch) == MAX_PATCH_CHARS
