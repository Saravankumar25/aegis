"""Prometheus HTTP API v1 client for the Prometheus MCP server.

Read-only by construction (query + alerts endpoints only). Local Prometheus is
unauthenticated by design (ESD §16 review, M2/M3); no credential is involved. An injected
``httpx.AsyncClient`` (contract tests use ``httpx.MockTransport``) replaces the real one.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from pydantic import ValidationError

from core.config import get_settings
from mcp_servers.common import MalformedResponseError, UpstreamRequestError, retry_transient
from mcp_servers.prometheus.models import (
    AlertSummary,
    InstantQueryResult,
    InstantSample,
    RangeQueryResult,
    RangeSeries,
)

SOURCE = "prometheus"


class PrometheusClient:
    """Thin adapter over the Prometheus HTTP API (Adapter pattern, ESD §24)."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._attempts = settings.mcp_retry_attempts
        self._base_delay = settings.mcp_retry_base_delay_seconds
        self._http = http or httpx.AsyncClient(
            base_url=settings.prometheus_url, timeout=settings.mcp_http_timeout_seconds
        )

    async def _get_data(
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
            body = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedResponseError(f"Prometheus returned unparseable JSON: {exc}") from exc
        # Prometheus can report query errors in-body with HTTP 200.
        if not isinstance(body, dict) or "status" not in body:
            raise MalformedResponseError("Prometheus response missing 'status' field")
        if body["status"] != "success":
            raise UpstreamRequestError(400, str(body.get("error", "query failed")))
        return body.get("data"), attempts

    async def query_metrics(
        self, query: str, time: str | None = None
    ) -> tuple[InstantQueryResult, int]:
        params: dict[str, Any] = {"query": query}
        if time:
            params["time"] = time
        data, attempts = await self._get_data("/api/v1/query", "query_metrics", params)
        try:
            samples = [
                InstantSample(
                    metric=r.get("metric", {}),
                    timestamp=r["value"][0],
                    value=r["value"][1],
                )
                for r in data.get("result", [])
            ]
            result = InstantQueryResult(
                query=query, result_type=data.get("resultType", ""), samples=samples
            )
        except (KeyError, TypeError, AttributeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected instant query shape: {exc}") from exc
        return result, attempts

    async def query_range_metrics(
        self, query: str, start: str, end: str, step: str = "30s"
    ) -> tuple[RangeQueryResult, int]:
        params = {"query": query, "start": start, "end": end, "step": step}
        data, attempts = await self._get_data("/api/v1/query_range", "query_range_metrics", params)
        try:
            series = [
                RangeSeries(metric=r.get("metric", {}), values=r.get("values", []))
                for r in data.get("result", [])
            ]
            result = RangeQueryResult(query=query, start=start, end=end, step=step, series=series)
        except (KeyError, TypeError, AttributeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected range query shape: {exc}") from exc
        return result, attempts

    async def list_alerts(self, namespace: str | None = None) -> tuple[list[AlertSummary], int]:
        """Firing alerts, scoped to a namespace unless explicitly asked for everything.

        Unscoped, this returns every alert in the cluster — including the monitoring stack's
        own control-plane alerts. That is not a hypothetical problem: `etcdMembersDown` from
        `kube-system` was repeatedly handed to RCA as evidence for a `checkout-service` error
        spike, and the model twice built its hypothesis around a control-plane failure that
        had nothing to do with the incident. The Observer rejected both, so nothing wrong was
        published, but two full investigations were spent on a distractor supplied by the
        evidence layer.

        Alerts carrying no `namespace` label are platform-level and are excluded when a scope
        is given, for the same reason. Passing ``namespace=None`` still returns everything,
        because a genuine cluster-wide investigation needs it — the default is scoped, the
        capability remains.
        """
        data, attempts = await self._get_data("/api/v1/alerts", "list_alerts")
        try:
            alerts = [
                AlertSummary(
                    name=a.get("labels", {}).get("alertname", ""),
                    state=a.get("state", ""),
                    labels=a.get("labels", {}),
                    annotations=a.get("annotations", {}),
                    active_at=a.get("activeAt"),
                    value=a.get("value"),
                )
                for a in data.get("alerts", [])
                if namespace is None or a.get("labels", {}).get("namespace") == namespace
            ]
        except (KeyError, TypeError, AttributeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected alerts shape: {exc}") from exc
        return alerts, attempts

    async def aclose(self) -> None:
        await self._http.aclose()
