"""Unit tests pinning enum values (ESD §6, CLAUDE.md §4/§15).

Enum values are persisted, so an accidental rename is a silent data-compatibility break. These tests
pin the exact value sets; changing one forces a deliberate edit here and a matching migration.
"""

from __future__ import annotations

from db.enums import (
    ActorType,
    AgentMessageType,
    AlertSource,
    EvidenceType,
    IncidentState,
    RunbookSource,
    Severity,
    UserRole,
)


def _values(enum_cls: type) -> set[str]:
    return {member.value for member in enum_cls}


def test_severity_values() -> None:
    assert _values(Severity) == {"P1", "P2", "P3", "P4"}


def test_incident_state_values() -> None:
    assert _values(IncidentState) == {
        "open",
        "investigating",
        "hypothesis_formed",
        # Terminal for automation, not for the incident (migration 0009). Added because an
        # escalated incident previously sat in `hypothesis_formed`, indistinguishable from
        # one still being worked — so the dashboard could not show which incidents were
        # waiting on a human.
        "escalated",
        "remediation_proposed",
        "remediation_approved",
        "remediation_executed",
        "monitoring",
        "resolved",
        "closed",
        "reopened",
    }


def test_actor_type_values() -> None:
    assert _values(ActorType) == {"agent", "human", "system"}


def test_alert_source_values() -> None:
    assert _values(AlertSource) == {"prometheus", "github", "pagerduty"}


def test_evidence_type_values() -> None:
    assert _values(EvidenceType) == {"log", "metric", "diff"}


def test_agent_message_type_values() -> None:
    assert _values(AgentMessageType) == {"reasoning", "action", "handoff"}


def test_user_role_values() -> None:
    assert _values(UserRole) == {"admin", "on_call_engineer", "viewer"}


def test_runbook_source_values() -> None:
    assert _values(RunbookSource) == {"internal", "postmortem_corpus"}


def test_enum_values_lowercase_except_severity() -> None:
    """CLAUDE.md §4: enum values are lowercase_with_underscores, except the P-code severities."""
    for enum_cls in (
        IncidentState,
        ActorType,
        AlertSource,
        EvidenceType,
        AgentMessageType,
        UserRole,
        RunbookSource,
    ):
        for member in enum_cls:
            assert member.value == member.value.lower()
