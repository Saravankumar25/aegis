"""GitHub REST API client for the GitHub MCP server.

Read-only by construction (GET only). Uses an optional ``GITHUB_TOKEN`` (per-server
credential, ESD §16); unauthenticated access works for public repos at a lower rate
limit. The default lookback window for recent commits is 2 hours (FR-2.2). An injected
``httpx.AsyncClient`` (contract tests use ``httpx.MockTransport``) replaces the real one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from core.config import get_settings
from mcp_servers.common import MalformedResponseError, retry_transient
from mcp_servers.github.models import CommitDiff, CommitSummary, FileDiff, PullRequestSummary

SOURCE = "github"

# Bound per-file patch text so one giant diff can't blow the token budget (ESD §15).
MAX_PATCH_CHARS = 4_000


class GitHubClient:
    """Thin adapter over the GitHub REST API (Adapter pattern, ESD §24)."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self.repo = settings.github_repo
        self._attempts = settings.mcp_retry_attempts
        self._base_delay = settings.mcp_retry_base_delay_seconds
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        self._http = http or httpx.AsyncClient(
            base_url=settings.github_api_url,
            headers=headers,
            timeout=settings.mcp_http_timeout_seconds,
        )

    async def _get_json(
        self, path: str, tool: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, int]:
        response, attempts = await retry_transient(
            lambda: self._http.get(path, params=params),
            attempts=self._attempts,
            base_delay_seconds=self._base_delay,
            source=SOURCE,
            tool=tool,
        )
        try:
            return response.json(), attempts
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedResponseError(f"GitHub returned unparseable JSON: {exc}") from exc

    async def get_recent_commits(
        self, lookback_hours: float = 2.0, branch: str | None = None, limit: int = 30
    ) -> tuple[list[CommitSummary], int]:
        since = (datetime.now(UTC) - timedelta(hours=lookback_hours)).isoformat()
        params: dict[str, Any] = {"since": since, "per_page": min(limit, 100)}
        if branch:
            params["sha"] = branch
        body, attempts = await self._get_json(
            f"/repos/{self.repo}/commits", "get_recent_commits", params
        )
        try:
            commits = [_commit_summary(item) for item in body]
        except (KeyError, TypeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected commit list shape: {exc}") from exc
        return commits, attempts

    async def list_pull_requests(
        self, state: str = "closed", limit: int = 20
    ) -> tuple[list[PullRequestSummary], int]:
        params = {
            "state": state,
            "sort": "updated",
            "direction": "desc",
            "per_page": min(limit, 100),
        }
        body, attempts = await self._get_json(
            f"/repos/{self.repo}/pulls", "list_pull_requests", params
        )
        try:
            pulls = [
                PullRequestSummary(
                    number=item["number"],
                    title=item.get("title", ""),
                    state=item.get("state", ""),
                    author=(item.get("user") or {}).get("login", ""),
                    merged_at=item.get("merged_at"),
                    updated_at=item.get("updated_at"),
                    head_branch=(item.get("head") or {}).get("ref"),
                )
                for item in body
            ]
        except (KeyError, TypeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected PR list shape: {exc}") from exc
        return pulls, attempts

    async def get_commit_diff(self, sha: str) -> tuple[CommitDiff, int]:
        body, attempts = await self._get_json(
            f"/repos/{self.repo}/commits/{sha}", "get_commit_diff"
        )
        try:
            summary = _commit_summary(body)
            files = [
                FileDiff(
                    filename=f["filename"],
                    status=f.get("status", ""),
                    additions=f.get("additions", 0),
                    deletions=f.get("deletions", 0),
                    patch=(f.get("patch") or None) and f["patch"][:MAX_PATCH_CHARS],
                )
                for f in body.get("files", [])
            ]
            stats = body.get("stats", {})
            diff = CommitDiff(
                sha=summary.sha,
                message=summary.message,
                author=summary.author,
                authored_at=summary.authored_at,
                files=files,
                total_additions=stats.get("additions", 0),
                total_deletions=stats.get("deletions", 0),
            )
        except (KeyError, TypeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected commit diff shape: {exc}") from exc
        return diff, attempts

    async def aclose(self) -> None:
        await self._http.aclose()


def _commit_summary(item: dict[str, Any]) -> CommitSummary:
    commit = item["commit"]
    author = commit.get("author") or {}
    return CommitSummary(
        sha=item["sha"],
        message=commit.get("message", ""),
        author=author.get("name", ""),
        authored_at=author.get("date", ""),
    )
