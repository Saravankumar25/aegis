"""Domain enums shared across models, agents, and the API (ESD §6).

Enum *values* are lowercase-with-underscores per CLAUDE.md §4, with the single documented exception
of ``Severity`` (P1-P4), whose uppercase codes are the industry-standard severity notation used
verbatim in PRD/ESD. ``StrEnum`` (Python 3.11+) keeps ``name == value`` for every member, so the
persisted label matches the enum both in SQLAlchemy and in the native Postgres enum type.
"""

from __future__ import annotations

from enum import StrEnum


class Severity(StrEnum):
    """Incident severity (PRD FR-1.3). Uppercase P-codes are the domain convention (ESD §6)."""

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class IncidentState(StrEnum):
    """Incident lifecycle states (ESD §6.1).

    MVP uses open→investigating→hypothesis_formed→monitoring/resolved→closed (+reopened);
    the remediation_* states are V1.5.
    """

    open = "open"
    investigating = "investigating"
    hypothesis_formed = "hypothesis_formed"
    remediation_proposed = "remediation_proposed"
    remediation_approved = "remediation_approved"
    remediation_executed = "remediation_executed"
    monitoring = "monitoring"
    resolved = "resolved"
    closed = "closed"
    reopened = "reopened"


class ActorType(StrEnum):
    """Who caused a transition / audit entry (ESD §6)."""

    agent = "agent"
    human = "human"
    system = "system"


class AlertSource(StrEnum):
    """Origin of an ingested alert. Real: prometheus/github; mocked: pagerduty (PRD §12)."""

    prometheus = "prometheus"
    github = "github"
    pagerduty = "pagerduty"


class EvidenceType(StrEnum):
    """Kind of evidence a citation points at (ESD §6, evidence_citations)."""

    log = "log"
    metric = "metric"
    diff = "diff"


class AgentMessageType(StrEnum):
    """Category of an agent message (ESD §6, agent_messages)."""

    reasoning = "reasoning"
    action = "action"
    handoff = "handoff"


class UserRole(StrEnum):
    """RBAC roles (ESD §8). Only on_call_engineer/admin may act on V1.5 proposals."""

    admin = "admin"
    on_call_engineer = "on_call_engineer"
    viewer = "viewer"


class RunbookSource(StrEnum):
    """Provenance of a runbook/postmortem document (ESD §6, runbooks)."""

    internal = "internal"
    postmortem_corpus = "postmortem_corpus"


class RemediationStatus(StrEnum):
    """Lifecycle of a remediation action (ESD §6, remediation_actions)."""

    proposed = "proposed"
    leased = "leased"
    approved = "approved"
    executed = "executed"
    rejected = "rejected"
    failed = "failed"
    rolled_back = "rolled_back"


class ApprovalDecision(StrEnum):
    """Human decision on a Tier-2 proposal (ESD §6, approvals)."""

    approved = "approved"
    rejected = "rejected"
