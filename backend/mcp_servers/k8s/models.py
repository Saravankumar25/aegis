"""Pydantic models for the k8s MCP server's tool outputs (CLAUDE.md §3: typed boundaries).

These are deliberately *summaries*, not raw k8s objects: the consumer is an LLM agent with a
hard token budget (ESD §15), so each model keeps only the fields an investigation actually
uses. Free-text fields (log text, event messages) are untrusted evidence (ESD §16).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ContainerStatus(BaseModel):
    """Per-container health, including the last termination reason (OOMKilled, Error...)."""

    name: str
    ready: bool
    restart_count: int
    image: str
    state: str = Field(description="running | waiting | terminated")
    state_reason: str | None = None  # e.g. CrashLoopBackOff, ImagePullBackOff
    last_terminated_reason: str | None = None  # e.g. OOMKilled, Error


class PodSummary(BaseModel):
    """One row of `list_pods`."""

    name: str
    namespace: str
    phase: str
    ready: str = Field(description='Ready containers as "n/m".')
    restarts: int
    node: str | None = None
    start_time: str | None = None


class PodDetail(PodSummary):
    """`get_pod` output — the describe-level view of a single pod."""

    labels: dict[str, str] = Field(default_factory=dict)
    containers: list[ContainerStatus] = Field(default_factory=list)
    conditions: dict[str, str] = Field(
        default_factory=dict, description="Condition type -> status, e.g. Ready -> False."
    )


class PodLogs(BaseModel):
    """`get_pod_logs` output. `text` is untrusted free text."""

    pod: str
    namespace: str
    container: str | None = None
    tail_lines: int
    text: str


class EventSummary(BaseModel):
    """One k8s Event. `message` is untrusted free text."""

    type: str  # Normal | Warning
    reason: str
    message: str
    involved_object: str = Field(description='"Kind/name", e.g. "Pod/checkout-abc".')
    count: int
    last_timestamp: str | None = None


class DeploymentSummary(BaseModel):
    """One row of `list_deployments` — replica health and running images."""

    name: str
    namespace: str
    replicas_desired: int
    replicas_ready: int
    replicas_available: int
    images: list[str] = Field(default_factory=list)
