"""Correlation Agent core: gather + correlate evidence across sources (FR-2.1..FR-2.3).

Pure orchestration over the gateway: every tool result is folded into the EvidenceStore
(which redacts + delimits on entry), unavailable sources become documented gaps instead of
failures (PRD 11A), and the output correlates across the **temporal** (deploys inside the
lookback window) and **topological** (affected service + its dependency neighborhood)
dimensions before handing off to RCA (FR-2.3).
"""

from __future__ import annotations

import json
from typing import Any

from agents.correlation.gaps import sanitize_gap_reason
from agents.evidence import EvidenceStore
from agents.topology import dependencies_of, dependents_of
from core.config import get_settings
from db.enums import EvidenceType


def _data(result: dict[str, Any]) -> Any:
    return result.get("data") if result.get("ok") else None


async def collect_evidence(gateway: Any, service_name: str) -> tuple[EvidenceStore, str]:
    """Gather logs, metrics, events, deploy history for one service (FR-2.1, FR-2.2).

    Returns ``(store, correlation_summary)``.
    """
    store = EvidenceStore()
    settings = get_settings()

    # --- k8s: pods, logs, events (topological scope: this service's pods) ---
    pods_result = await gateway.call("k8s", "list_pods", {})
    service_pods: list[dict] = []
    if (pods := _data(pods_result)) is not None:
        service_pods = [p for p in pods if p["name"].startswith(service_name)]
        summary = (
            "\n".join(
                f"pod {p['name']} phase={p['phase']} ready={p['ready']} restarts={p['restarts']}"
                for p in service_pods
            )
            or f"no pods found for {service_name}"
        )
        store.add(
            type_=EvidenceType.log,
            source="k8s.list_pods",
            ref=f"k8s/pods/{service_name}",
            text=summary,
        )
    else:
        store.note_gap(
            "k8s.list_pods", sanitize_gap_reason(pods_result.get("error") or "unavailable")
        )

    if service_pods:
        pod_name = service_pods[0]["name"]
        logs_result = await gateway.call(
            "k8s", "get_pod_logs", {"name": pod_name, "tail_lines": 100}
        )
        if (logs := _data(logs_result)) is not None:
            store.add(
                type_=EvidenceType.log,
                source="k8s.get_pod_logs",
                ref=f"k8s/pod/{pod_name}/log",
                text=logs["text"] or "(empty log)",
            )
        else:
            store.note_gap(
                "k8s.get_pod_logs", sanitize_gap_reason(logs_result.get("error") or "unavailable")
            )

    events_result = await gateway.call("k8s", "list_events", {})
    if (events := _data(events_result)) is not None:
        relevant = [
            e
            for e in events
            if service_name in e.get("involved_object", "") or e.get("type") == "Warning"
        ][:20]
        if relevant:
            store.add(
                type_=EvidenceType.log,
                source="k8s.list_events",
                ref=f"k8s/events/{service_name}",
                text="\n".join(
                    f"[{e['type']}] {e['reason']} x{e['count']}: {e['message']}" for e in relevant
                ),
            )
    else:
        store.note_gap(
            "k8s.list_events", sanitize_gap_reason(events_result.get("error") or "unavailable")
        )

    # --- prometheus: error rate + latency for the service's pods ---
    error_q = (
        f'sum by (status) (rate(http_requests_total{{namespace="meridian",'
        f'pod=~"{service_name}.*"}}[5m]))'
    )
    metrics_result = await gateway.call("prometheus", "query_metrics", {"query": error_q})
    if (metrics := _data(metrics_result)) is not None:
        lines = [
            f"rate(status={s['metric'].get('status', '?')}) = {s['value']}/s"
            for s in metrics.get("samples", [])
        ]
        store.add(
            type_=EvidenceType.metric,
            source="prometheus.query_metrics",
            ref=f"prom/error_rate/{service_name}",
            text="\n".join(lines) or "no request-rate samples returned",
        )
    else:
        store.note_gap(
            "prometheus.query_metrics",
            sanitize_gap_reason(metrics_result.get("error") or "unavailable"),
        )

    alerts_result = await gateway.call("prometheus", "list_alerts", {})
    if (alerts := _data(alerts_result)) is not None:
        firing = [a for a in alerts if a.get("state") == "firing"][:10]
        if firing:
            store.add(
                type_=EvidenceType.metric,
                source="prometheus.list_alerts",
                ref="prom/alerts",
                text="\n".join(f"{a['name']} [{a['state']}] {a.get('labels')}" for a in firing),
            )
    else:
        store.note_gap(
            "prometheus.list_alerts",
            sanitize_gap_reason(alerts_result.get("error") or "unavailable"),
        )

    # --- github: recent deploys/changes (temporal dimension, FR-2.2) ---
    commits_result = await gateway.call(
        "github",
        "get_recent_commits",
        {"lookback_hours": settings.deploy_lookback_hours},
    )
    if (commits := _data(commits_result)) is not None:
        if commits:
            store.add(
                type_=EvidenceType.diff,
                source="github.get_recent_commits",
                ref="github/commits/recent",
                text="\n".join(
                    f"commit {c['sha'][:7]} at {c['authored_at']}: {c['message'].splitlines()[0]}"
                    for c in commits[:10]
                ),
            )
        else:
            # Phrased without change-related keywords so absence of changes can never be
            # pattern-matched into a change-related signal downstream.
            store.add(
                type_=EvidenceType.diff,
                source="github.get_recent_commits",
                ref="github/commits/recent",
                text=f"repository quiet for the last {settings.deploy_lookback_hours}h; "
                f"nothing shipped in the window",
            )
    else:
        store.note_gap(
            "github.get_recent_commits",
            sanitize_gap_reason(commits_result.get("error") or "unavailable"),
        )

    # --- correlate: temporal × topological (FR-2.3) ---
    correlation = {
        "service": service_name,
        "topology": {
            "depends_on": dependencies_of(service_name),
            "depended_on_by": dependents_of(service_name),
        },
        "temporal": {
            "deploy_lookback_hours": settings.deploy_lookback_hours,
            "recent_change_evidence": [i.id for i in store.items if i.type == EvidenceType.diff],
        },
        "evidence_ids": [i.id for i in store.items],
        "gaps": store.gaps,
    }
    return store, json.dumps(correlation)
