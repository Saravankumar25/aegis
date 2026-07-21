"""Read-only Kubernetes API client for the k8s MCP server.

Talks plain REST (GET only, by construction) to the API server, authenticated with the
dedicated `aegis-k8s-mcp` ServiceAccount token (ESD §16: per-server, least-privilege
credential — the M2 RBAC enforces read-only server-side even if this client had a bug).
The token file is re-read on every call so a rotated token needs no restart. TLS is
verified against the cluster CA extracted by infra/gen-mcp-credentials.sh.

An injected ``httpx.AsyncClient`` (contract tests use ``httpx.MockTransport``) bypasses
the credential files entirely.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from core.config import get_settings
from mcp_servers.common import MalformedResponseError, SourceUnavailableError, retry_transient
from mcp_servers.k8s.models import (
    ContainerStatus,
    DeploymentSummary,
    EventSummary,
    PodDetail,
    PodLogs,
    PodSummary,
)

SOURCE = "k8s"


class K8sClient:
    """Thin, read-only adapter over the Kubernetes REST API (Adapter pattern, ESD §24)."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._token_file = Path(settings.k8s_token_file)
        self._writer_token_file = Path(settings.k8s_writer_token_file)
        self._attempts = settings.mcp_retry_attempts
        self._base_delay = settings.mcp_retry_base_delay_seconds
        if http is not None:
            self._http = http
        else:
            verify: str | bool = settings.k8s_ca_cert_file
            self._http = httpx.AsyncClient(
                base_url=settings.k8s_api_url,
                verify=verify,
                timeout=settings.mcp_http_timeout_seconds,
            )
        self._injected = http is not None

    def _headers(self, *, writer: bool = False) -> dict[str, str]:
        """Read tools use the read-only SA; write tools use the separate writer SA
        (V1.5, ESD §16). A missing writer token surfaces as SourceUnavailable — the write
        capability simply doesn't exist until the operator mints it."""
        headers = {"Accept": "application/json"}
        if not self._injected:
            token_file = self._writer_token_file if writer else self._token_file
            if writer and not token_file.exists():
                raise SourceUnavailableError(
                    "k8s writer credential not minted (run infra/gen-mcp-credentials.sh "
                    "after applying mcp-rbac-writer.yaml)"
                )
            token = token_file.read_text(encoding="utf-8").strip()
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _get(
        self, path: str, tool: str, params: dict[str, Any] | None = None
    ) -> tuple[httpx.Response, int]:
        return await retry_transient(
            lambda: self._http.get(path, params=params, headers=self._headers()),
            attempts=self._attempts,
            base_delay_seconds=self._base_delay,
            source=SOURCE,
            tool=tool,
        )

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise MalformedResponseError(f"k8s API returned unparseable JSON: {exc}") from exc

    async def list_pods(self, namespace: str) -> tuple[list[PodSummary], int]:
        response, attempts = await self._get(
            f"/api/v1/namespaces/{namespace}/pods", tool="list_pods", params={"limit": 200}
        )
        body = self._json(response)
        try:
            pods = [_pod_summary(item) for item in body["items"]]
        except (KeyError, TypeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected pod list shape: {exc}") from exc
        return pods, attempts

    async def get_pod(self, name: str, namespace: str) -> tuple[PodDetail, int]:
        response, attempts = await self._get(
            f"/api/v1/namespaces/{namespace}/pods/{name}", tool="get_pod"
        )
        body = self._json(response)
        try:
            return _pod_detail(body), attempts
        except (KeyError, TypeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected pod shape: {exc}") from exc

    async def get_pod_logs(
        self,
        name: str,
        namespace: str,
        container: str | None = None,
        tail_lines: int = 200,
    ) -> tuple[PodLogs, int]:
        params: dict[str, Any] = {"tailLines": tail_lines}
        if container:
            params["container"] = container
        response, attempts = await self._get(
            f"/api/v1/namespaces/{namespace}/pods/{name}/log", tool="get_pod_logs", params=params
        )
        logs = PodLogs(
            pod=name,
            namespace=namespace,
            container=container,
            tail_lines=tail_lines,
            text=response.text,
        )
        return logs, attempts

    async def list_events(self, namespace: str) -> tuple[list[EventSummary], int]:
        response, attempts = await self._get(
            f"/api/v1/namespaces/{namespace}/events", tool="list_events", params={"limit": 200}
        )
        body = self._json(response)
        try:
            events = [_event_summary(item) for item in body["items"]]
        except (KeyError, TypeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected event list shape: {exc}") from exc
        return events, attempts

    async def list_deployments(self, namespace: str) -> tuple[list[DeploymentSummary], int]:
        response, attempts = await self._get(
            f"/apis/apps/v1/namespaces/{namespace}/deployments",
            tool="list_deployments",
            params={"limit": 200},
        )
        body = self._json(response)
        try:
            deployments = [_deployment_summary(item) for item in body["items"]]
        except (KeyError, TypeError, ValidationError) as exc:
            raise MalformedResponseError(f"unexpected deployment list shape: {exc}") from exc
        return deployments, attempts

    # --- V1.5 write operations (writer SA only; forward actions of Tier-1/2) -----------

    async def restart_pod(self, name: str, namespace: str) -> tuple[dict[str, Any], int]:
        """Delete one pod; its Deployment recreates it (the Tier-1 'restart')."""
        response, attempts = await retry_transient(
            lambda: self._http.delete(
                f"/api/v1/namespaces/{namespace}/pods/{name}",
                headers=self._headers(writer=True),
            ),
            attempts=1,  # deletes are NOT retried blindly: one attempt, caller decides
            base_delay_seconds=self._base_delay,
            source=SOURCE,
            tool="restart_pod",
        )
        body = self._json(response)
        return {"deleted": name, "namespace": namespace, "status": body.get("status")}, attempts

    async def scale_deployment(
        self, name: str, replicas: int, namespace: str
    ) -> tuple[dict[str, Any], int]:
        """Patch the /scale subresource — the replicas-only write surface (ESD §16)."""
        current, _ = await retry_transient(
            lambda: self._http.get(
                f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
                headers=self._headers(writer=True),
            ),
            attempts=self._attempts,
            base_delay_seconds=self._base_delay,
            source=SOURCE,
            tool="scale_deployment",
        )
        previous = self._json(current).get("spec", {}).get("replicas")
        response, attempts = await retry_transient(
            lambda: self._http.patch(
                f"/apis/apps/v1/namespaces/{namespace}/deployments/{name}/scale",
                headers={
                    **self._headers(writer=True),
                    "Content-Type": "application/merge-patch+json",
                },
                content=json.dumps({"spec": {"replicas": replicas}}),
            ),
            attempts=1,  # writes get one attempt; idempotency lives in the action layer
            base_delay_seconds=self._base_delay,
            source=SOURCE,
            tool="scale_deployment",
        )
        body = self._json(response)
        return {
            "deployment": name,
            "namespace": namespace,
            "previous_replicas": previous,
            "replicas": body.get("spec", {}).get("replicas"),
        }, attempts

    async def aclose(self) -> None:
        await self._http.aclose()


def _pod_summary(item: dict[str, Any]) -> PodSummary:
    meta, status = item["metadata"], item.get("status", {})
    statuses = status.get("containerStatuses") or []
    ready_count = sum(1 for c in statuses if c.get("ready"))
    return PodSummary(
        name=meta["name"],
        namespace=meta["namespace"],
        phase=status.get("phase", "Unknown"),
        ready=f"{ready_count}/{len(statuses)}",
        restarts=sum(c.get("restartCount", 0) for c in statuses),
        node=item.get("spec", {}).get("nodeName"),
        start_time=status.get("startTime"),
    )


def _container_status(raw: dict[str, Any]) -> ContainerStatus:
    state = raw.get("state") or {}
    state_name = next(iter(state), "unknown")
    last_state = (raw.get("lastState") or {}).get("terminated") or {}
    return ContainerStatus(
        name=raw["name"],
        ready=raw.get("ready", False),
        restart_count=raw.get("restartCount", 0),
        image=raw.get("image", ""),
        state=state_name,
        state_reason=(state.get(state_name) or {}).get("reason"),
        last_terminated_reason=last_state.get("reason"),
    )


def _pod_detail(item: dict[str, Any]) -> PodDetail:
    summary = _pod_summary(item)
    status = item.get("status", {})
    return PodDetail(
        **summary.model_dump(),
        labels=item["metadata"].get("labels") or {},
        containers=[_container_status(c) for c in status.get("containerStatuses") or []],
        conditions={c["type"]: c["status"] for c in status.get("conditions") or [] if "type" in c},
    )


def _event_summary(item: dict[str, Any]) -> EventSummary:
    involved = item.get("involvedObject", {})
    return EventSummary(
        type=item.get("type", "Unknown"),
        reason=item.get("reason", ""),
        message=item.get("message", ""),
        involved_object=f"{involved.get('kind', '?')}/{involved.get('name', '?')}",
        count=item.get("count", 1),
        last_timestamp=item.get("lastTimestamp"),
    )


def _deployment_summary(item: dict[str, Any]) -> DeploymentSummary:
    meta, status = item["metadata"], item.get("status", {})
    containers = item.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    return DeploymentSummary(
        name=meta["name"],
        namespace=meta["namespace"],
        replicas_desired=item.get("spec", {}).get("replicas", 0),
        replicas_ready=status.get("readyReplicas", 0),
        replicas_available=status.get("availableReplicas", 0),
        images=[c.get("image", "") for c in containers],
    )
