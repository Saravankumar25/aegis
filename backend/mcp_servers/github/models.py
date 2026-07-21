"""Pydantic models for the GitHub MCP server's tool outputs (typed boundaries).

Commit messages, PR titles, and diff patches are authored by whoever pushed the code —
untrusted free text by default (ESD §16); patch sizes are capped so a giant diff cannot
blow the incident token budget (ESD §15).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CommitSummary(BaseModel):
    """One row of `get_recent_commits`. `message` is untrusted free text."""

    sha: str
    message: str
    author: str
    authored_at: str


class PullRequestSummary(BaseModel):
    """One row of `list_pull_requests`. `title` is untrusted free text."""

    number: int
    title: str
    state: str
    author: str
    merged_at: str | None = None
    updated_at: str | None = None
    head_branch: str | None = None


class FileDiff(BaseModel):
    """One changed file within `get_commit_diff`. `patch` is untrusted free text."""

    filename: str
    status: str  # added | modified | removed | renamed
    additions: int
    deletions: int
    patch: str | None = Field(
        default=None, description="Unified diff hunk; truncated to a bounded length."
    )


class CommitDiff(BaseModel):
    """`get_commit_diff` output — the describe-level view of one commit."""

    sha: str
    message: str
    author: str
    authored_at: str
    files: list[FileDiff] = Field(default_factory=list)
    total_additions: int = 0
    total_deletions: int = 0
