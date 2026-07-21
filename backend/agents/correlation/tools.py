"""The evidence-gathering tool catalog the Correlation agent reasons over (FR-2.1, ESD §3).

This is an **allowlist**, and that is the security boundary, not a convenience. The model
proposes tool calls by name; nothing dispatches unless the name appears here. A denylist would
mean any tool added to an MCP server later becomes model-callable the moment it exists —
including `restart_pod` and `scale_deployment`, which mutate the cluster. Evidence gathering
must be incapable of causing a write, regardless of what a prompt-injected log line asks for.

Each entry carries a `when_to_use` line rather than only a description. A model choosing
between `get_pod_logs` and `list_events` needs to know *which question each answers*, not what
each returns; without that the selection collapses into "call everything", which is the fixed
sequence this replaces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from db.enums import EvidenceType


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One evidence tool the model may call."""

    server: str
    tool: str
    description: str
    when_to_use: str
    evidence_type: EvidenceType
    # JSON-schema-ish argument declaration, rendered into the prompt.
    args: dict[str, str] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.server}.{self.tool}"


READ_ONLY_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        server="k8s",
        tool="list_pods",
        description="List pods with phase, readiness (ready/total) and restart counts.",
        when_to_use=(
            "Almost always first: it establishes which pods exist and whether any are "
            "unhealthy, and its output supplies the pod names other k8s tools need."
        ),
        evidence_type=EvidenceType.log,
        args={"namespace": "string, optional — defaults to the Meridian namespace"},
    ),
    ToolSpec(
        server="k8s",
        tool="get_pod",
        description="Describe one pod: container states, restart and termination reasons.",
        when_to_use=(
            "When list_pods shows a pod restarting or not ready and you need the *reason* "
            "(OOMKilled, CrashLoopBackOff, image pull failure)."
        ),
        evidence_type=EvidenceType.log,
        args={"name": "string — exact pod name from list_pods", "namespace": "string, optional"},
        required=("name",),
    ),
    ToolSpec(
        server="k8s",
        tool="get_pod_logs",
        description="Recent log lines from one pod.",
        when_to_use=(
            "When you need the application's own account of a failure — stack traces, "
            "connection errors, timeouts. Costly and noisy: prefer it after you know which "
            "pod is unhealthy."
        ),
        evidence_type=EvidenceType.log,
        args={
            "name": "string — exact pod name",
            "tail_lines": "integer, optional (default 100)",
            "namespace": "string, optional",
        },
        required=("name",),
    ),
    ToolSpec(
        server="k8s",
        tool="list_events",
        description="Recent cluster events (scheduling, evictions, probe failures, OOM kills).",
        when_to_use=(
            "When pods are unhealthy but their logs are unrevealing — events explain what the "
            "cluster did *to* the pod rather than what the app did."
        ),
        evidence_type=EvidenceType.log,
        args={"namespace": "string, optional"},
    ),
    ToolSpec(
        server="k8s",
        tool="list_deployments",
        description="Deployments with desired/ready/available replicas and running image tags.",
        when_to_use=(
            "When you suspect a capacity problem, or need the running image tag to tie the "
            "incident to a specific release."
        ),
        evidence_type=EvidenceType.log,
        args={"namespace": "string, optional"},
    ),
    ToolSpec(
        server="prometheus",
        tool="query_metrics",
        description="Instant PromQL query — the metric's value right now.",
        when_to_use=(
            "To quantify the symptom: current error rate, request rate, memory usage. Use a "
            "PromQL expression scoped to the affected service."
        ),
        evidence_type=EvidenceType.metric,
        args={"query": "string — a valid PromQL expression"},
        required=("query",),
    ),
    ToolSpec(
        server="prometheus",
        tool="query_range_metrics",
        description="Range PromQL query — how a metric moved over a time window.",
        when_to_use=(
            "To establish *when* a change began, which is what ties a symptom to a deploy or "
            "a config change. An instant query cannot show onset."
        ),
        evidence_type=EvidenceType.metric,
        args={
            "query": "string — a valid PromQL expression",
            "minutes": "integer, optional — lookback window (default 30)",
        },
        required=("query",),
    ),
    ToolSpec(
        server="prometheus",
        tool="list_alerts",
        description="Currently firing Prometheus alerts across the platform.",
        when_to_use=(
            "To see whether this incident is isolated or one symptom of a broader failure — "
            "several services alerting together points away from a single-service cause."
        ),
        evidence_type=EvidenceType.metric,
        args={},
    ),
    ToolSpec(
        server="github",
        tool="get_recent_commits",
        description="Commits merged in a recent lookback window.",
        when_to_use=(
            "Whenever a deploy or code change is a candidate cause. Without this evidence a "
            "change-related root cause may NOT be asserted at all."
        ),
        evidence_type=EvidenceType.diff,
        args={"lookback_hours": "number, optional — defaults to the configured change window"},
    ),
    ToolSpec(
        server="github",
        tool="get_commit_diff",
        description="The actual file changes in one commit.",
        when_to_use=(
            "After get_recent_commits identifies a suspicious commit and you need to know "
            "whether it plausibly touches the failing behaviour."
        ),
        evidence_type=EvidenceType.diff,
        args={"sha": "string — commit sha from get_recent_commits"},
        required=("sha",),
    ),
)

TOOLS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in READ_ONLY_TOOLS}


def render_catalog() -> str:
    """Render the catalog for the planning prompt."""
    lines = []
    for spec in READ_ONLY_TOOLS:
        arg_text = (
            ", ".join(f"{k} ({v})" for k, v in spec.args.items()) if spec.args else "no arguments"
        )
        required = f" REQUIRED: {', '.join(spec.required)}." if spec.required else ""
        lines.append(
            f"- {spec.name}\n"
            f"    what it returns: {spec.description}\n"
            f"    when to use it: {spec.when_to_use}\n"
            f"    arguments: {arg_text}.{required}"
        )
    return "\n".join(lines)


def validate_call(name: str, arguments: dict[str, Any]) -> str | None:
    """Return an error string if this call may not be dispatched, else None.

    Rejection is explicit rather than silent so the model can be told *why* and correct
    itself on the next round — a silently dropped call looks to the model like a tool that
    returned nothing, and it will happily reason from that absence.
    """
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return f"'{name}' is not an available tool"
    missing = [a for a in spec.required if not arguments.get(a)]
    if missing:
        return f"'{name}' requires argument(s): {', '.join(missing)}"
    unexpected = set(arguments) - set(spec.args)
    if unexpected:
        return f"'{name}' does not accept argument(s): {', '.join(sorted(unexpected))}"
    return None
