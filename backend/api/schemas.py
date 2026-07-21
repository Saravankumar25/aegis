"""Pydantic schemas for every API boundary (ESD §16: input validation on every boundary)."""

from __future__ import annotations

import datetime
import uuid

from pydantic import BaseModel, Field

from db.enums import AgentMessageType, AlertSource, IncidentState, Severity


class AlertIn(BaseModel):
    """Incoming alert webhook payload (POST /incidents, FR-1.1)."""

    alert_source: AlertSource
    external_alert_id: str = Field(min_length=1, max_length=256)
    service_name: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    kind: str = Field(
        default="other",
        description="error_rate | latency | pod_crash | availability | other",
        max_length=32,
    )
    value: float | None = Field(default=None, description="Metric value behind the alert.")
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    starts_at: datetime.datetime | None = None


class IngestResult(BaseModel):
    """Response for alert ingestion: created, merged-duplicate, or already-known."""

    incident_id: uuid.UUID
    created: bool
    deduplicated: bool = False
    severity: Severity
    state: IncidentState


class IncidentOut(BaseModel):
    """One incident row (list + detail views)."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    title: str
    service_name: str
    severity: Severity
    state: IncidentState
    alert_source: AlertSource
    external_alert_id: str
    created_at: datetime.datetime
    updated_at: datetime.datetime
    resolved_at: datetime.datetime | None


class AgentMessageOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    agent_name: str
    message_type: AgentMessageType
    content: str
    message_metadata: dict | None
    created_at: datetime.datetime


class CitationOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    evidence_type: str
    evidence_ref: str
    evidence_snippet_redacted: str | None
    validated_by_observer: bool


class AgentStepOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    agent_name: str
    ensemble_pass_index: int | None
    input_summary: str | None
    output_summary: str | None
    structured_output: dict | None
    confidence: float | None
    model_used: str | None
    tokens_used: int | None
    cost_usd: float | None
    latency_ms: int | None
    created_at: datetime.datetime
    citations: list[CitationOut] = Field(default_factory=list)


class TransitionOut(BaseModel):
    model_config = {"from_attributes": True}

    from_state: IncidentState
    to_state: IncidentState
    actor_type: str
    actor_id: str
    created_at: datetime.datetime


class IncidentDetailOut(IncidentOut):
    """Full incident detail: row + messages + steps + transitions."""

    transitions: list[TransitionOut] = Field(default_factory=list)
    steps: list[AgentStepOut] = Field(default_factory=list)
    messages: list[AgentMessageOut] = Field(default_factory=list)


class ReplayEventOut(BaseModel):
    """One ordered replay event, reconstructed purely from persisted rows (FR-9.2)."""

    sequence: int
    at: datetime.datetime
    kind: str  # transition | step | message
    agent_name: str | None
    summary: str
    detail: dict


class ReplayOut(BaseModel):
    incident: IncidentOut
    events: list[ReplayEventOut]


class SessionExchangeIn(BaseModel):
    """POST /auth/session — a Firebase ID token, exchanged for an Aegis session cookie.

    The bound is a sanity check against absurd payloads, not a security control: the token is
    only ever trusted after server-side signature verification (ESD §8).
    """

    id_token: str = Field(min_length=1, max_length=8192)


class UserOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    email: str
    role: str
    display_name: str | None = None
    photo_url: str | None = None


class RunbookHit(BaseModel):
    """One RAG search result (GET /runbooks/search).

    A *chunk*, not a document: `id` identifies the passage and `citation` names the section it
    came from, so a citation points a responder at the paragraph rather than the file.
    """

    id: uuid.UUID
    runbook_id: uuid.UUID
    title: str
    heading_path: str
    citation: str
    snippet: str
    score: float
    source: str
    service_tags: list[str]
    # Which retrievers surfaced this chunk ("semantic", "lexical", or both) — makes a
    # surprising result diagnosable instead of merely surprising.
    matched_by: list[str]


# --- V1.5 schemas (approvals, safety, memory) ---


class RemediationActionOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    incident_id: uuid.UUID
    tier: int
    action_type: str
    target_resource_type: str
    target_resource_id: str
    status: str
    reasoning: str
    blast_radius: dict
    compensating_action: dict
    parameters: dict
    shadow: bool
    proposed_at: datetime.datetime
    expires_at: datetime.datetime
    executed_at: datetime.datetime | None
    result: dict | None


class PendingApprovalOut(BaseModel):
    action: RemediationActionOut
    incident_title: str
    service_name: str
    severity: Severity


class ApprovalIn(BaseModel):
    action_id: uuid.UUID
    decision: str = Field(pattern="^(approved|rejected)$")
    notes: str | None = Field(default=None, max_length=2000)


class KillSwitchIn(BaseModel):
    engaged: bool


class BreakerStatusOut(BaseModel):
    tripped: bool
    open_trips: int
    window_count: int


class MemorySummaryOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    incident_id: uuid.UUID
    service_name: str
    incident_type: str
    symptom: str
    root_cause: str
    fix: str
    outcome: str
    approved_by: uuid.UUID | None
    created_at: datetime.datetime


class MemoryApproveIn(BaseModel):
    edits: dict[str, str] | None = None
