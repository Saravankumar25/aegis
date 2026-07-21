"""Shared plumbing for the read-only MCP servers (ESD §12 graceful degradation).

This is a sibling *library* module, not a server: each MCP server imports it plus its own
package, and never another server's code (CLAUDE.md §9). It provides:

- ``ToolResult`` — the uniform envelope every tool returns. A tool call never raises across
  the MCP boundary; upstream failure becomes ``ok=False`` with a machine-readable
  ``error_kind`` so agents continue the investigation with a documented gap (ESD §12).
- ``retry_transient`` — exponential-backoff retry (default 3 attempts) applied only to
  *transient* failures: connect errors, timeouts, HTTP 5xx. Non-transient outcomes
  (4xx, malformed response bodies) fail fast — retrying them cannot succeed and only
  burns the incident's time budget.
- ``guarded`` — wraps a tool coroutine so any classified failure is converted into the
  envelope instead of an exception, keeping the server process healthy for the next call.

Free-text fields fetched from infrastructure (pod logs, event messages, commit messages,
diffs) are untrusted evidence (ESD §16); tools set ``contains_untrusted_text=True`` so the
consumer-side redaction + ``<evidence>`` delimiting boundary cannot be skipped silently.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, Field

from core.logging import get_logger


class ErrorKind(StrEnum):
    """Machine-readable failure classification for ``ToolResult.error_kind``."""

    unavailable = "unavailable"  # transient failures exhausted retries (ESD §12)
    not_found = "not_found"  # upstream said 404 — a valid answer, source still healthy
    bad_request = "bad_request"  # our query was rejected (4xx other than 404)
    malformed_response = "malformed_response"  # 200 but body didn't parse/validate


class SourceUnavailableError(Exception):
    """Raised internally after transient retries are exhausted; never crosses the boundary."""


class UpstreamRequestError(Exception):
    """Raised internally for non-transient upstream rejections (4xx)."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(f"upstream returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class MalformedResponseError(Exception):
    """Raised internally when a 2xx response body fails to parse or validate."""


class ToolResult(BaseModel):
    """Uniform envelope returned by every MCP tool (never an exception)."""

    ok: bool
    source: str  # "k8s" | "prometheus" | "github"
    tool: str  # verb_noun tool name
    error_kind: ErrorKind | None = None
    error: str | None = None
    # True when `data` includes free text authored outside Aegis (logs, messages, diffs);
    # consumers must redact + delimit before any prompt (ESD §16).
    contains_untrusted_text: bool = False
    data: Any = None
    attempts: int = Field(default=1, description="Upstream attempts made, including retries.")


def _is_transient(exc: Exception) -> bool:
    """Transient = worth retrying: network/timeout errors and HTTP 5xx."""
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


async def retry_transient(
    call: Callable[[], Awaitable[httpx.Response]],
    *,
    attempts: int = 3,
    base_delay_seconds: float = 0.2,
    source: str = "",
    tool: str = "",
) -> tuple[httpx.Response, int]:
    """Run ``call`` with exponential backoff on transient failures (ESD §12).

    Returns ``(response, attempts_used)`` on success. Raises ``SourceUnavailableError`` once
    transient retries are exhausted, ``UpstreamRequestError`` immediately on non-transient
    4xx responses.
    """
    log = get_logger(source=source, tool=tool)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = await call()
            if response.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"server error {response.status_code}",
                    request=response.request,
                    response=response,
                )
            if response.status_code >= 400:
                raise UpstreamRequestError(response.status_code, response.text[:500])
            return response, attempt
        except UpstreamRequestError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            if not _is_transient(exc):
                raise
            last_exc = exc
            if attempt < attempts:
                delay = base_delay_seconds * (2 ** (attempt - 1))
                log.warning(
                    "mcp_upstream_transient_failure",
                    attempt=attempt,
                    max_attempts=attempts,
                    retry_in_seconds=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)
    log.error("mcp_source_unavailable", attempts=attempts, error=str(last_exc))
    raise SourceUnavailableError(f"{source or 'source'} unavailable after {attempts} attempts")


async def guarded(
    source: str,
    tool: str,
    fetch: Callable[[], Awaitable[ToolResult]],
) -> ToolResult:
    """Execute a tool body, converting classified failures into a ``ToolResult`` envelope.

    Unexpected exceptions still propagate (CLAUDE.md §3: no blanket swallowing) — the MCP
    framework surfaces them as tool errors and they indicate a bug, not upstream weather.
    """
    try:
        return await fetch()
    except SourceUnavailableError as exc:
        return ToolResult(
            ok=False, source=source, tool=tool, error_kind=ErrorKind.unavailable, error=str(exc)
        )
    except UpstreamRequestError as exc:
        kind = ErrorKind.not_found if exc.status_code == 404 else ErrorKind.bad_request
        return ToolResult(ok=False, source=source, tool=tool, error_kind=kind, error=str(exc))
    except MalformedResponseError as exc:
        return ToolResult(
            ok=False,
            source=source,
            tool=tool,
            error_kind=ErrorKind.malformed_response,
            error=str(exc),
        )
