"""Integration tests: Resolution agent — tiers, gates, shadow mode, compensating reversal.

The compensating-action test uses a stateful fake k8s gateway so the assertion is about
real state change and its reversal, not just that the forward call happened (CLAUDE.md §7).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from agents.resolution.engine import (
    execute_action,
    execute_compensating_action,
    propose_remediation,
)
from core.config import get_settings
from db.enums import AlertSource, IncidentState, RemediationStatus, Severity
from db.models import Incident
from db.repository import IncidentRepository
from safety.kill_switch.switch import set_kill_switch


def _decision(action_type: str, **parameters):
    """A fixed Resolution decision, so these tests exercise the gates rather than selection.

    Action choice is the Resolution agent's LLM reasoning (covered in
    `tests/unit/test_resolution_planner.py`). What is under test here is everything that
    happens *after* a choice: tiering, expiry, shadow mode, the four execution gates and the
    compensating action. Pinning the choice keeps a model-behaviour change from turning these
    safety assertions red for an unrelated reason.
    """
    from agents.resolution.actions import CATALOG
    from agents.resolution.planner import ResolutionDecision

    return ResolutionDecision(
        spec=CATALOG[action_type],
        reasoning=f"test fixture selected {action_type}",
        confidence=0.9,
        parameters=parameters,
        model_used="test-model",
    )



def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class StatefulK8sGateway:
    """Fake k8s that tracks deployment replica state and records every write."""

    def __init__(self, deployments: dict[str, int] | None = None) -> None:
        self.deployments = deployments or {"checkout-service": 2, "payment-service": 2}
        self.writes: list[tuple[str, dict]] = []

    async def call(self, server: str, tool: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        if tool == "list_deployments":
            data = [
                {
                    "name": name,
                    "namespace": "meridian",
                    "replicas_desired": count,
                    "replicas_ready": count,
                    "replicas_available": count,
                    "images": [],
                }
                for name, count in self.deployments.items()
            ]
            return {
                "ok": True,
                "source": "k8s",
                "tool": tool,
                "data": data,
                "error_kind": None,
                "error": None,
                "contains_untrusted_text": False,
                "attempts": 1,
            }
        if tool == "scale_deployment":
            self.writes.append((tool, arguments))
            previous = self.deployments.get(arguments["name"])
            self.deployments[arguments["name"]] = arguments["replicas"]
            return {
                "ok": True,
                "source": "k8s",
                "tool": tool,
                "data": {
                    "deployment": arguments["name"],
                    "previous_replicas": previous,
                    "replicas": arguments["replicas"],
                },
                "error_kind": None,
                "error": None,
                "contains_untrusted_text": False,
                "attempts": 1,
            }
        if tool == "restart_pod":
            self.writes.append((tool, arguments))
            return {
                "ok": True,
                "source": "k8s",
                "tool": tool,
                "data": {"deleted": arguments["name"]},
                "error_kind": None,
                "error": None,
                "contains_untrusted_text": False,
                "attempts": 1,
            }
        return {
            "ok": False,
            "source": server,
            "tool": tool,
            "error_kind": "unavailable",
            "error": "no fixture",
            "data": None,
            "contains_untrusted_text": False,
            "attempts": 0,
        }


async def _incident(
    session: AsyncSession, service: str, state: IncidentState = IncidentState.hypothesis_formed
) -> Incident:
    repo = IncidentRepository(session)
    incident, _ = await repo.upsert_incident(
        external_alert_id=f"res-{uuid.uuid4().hex[:8]}",
        alert_source=AlertSource.prometheus,
        title=f"{service} incident",
        service_name=service,
        severity=Severity.P1,
    )
    if state != IncidentState.open:
        await repo.record_transition(
            incident, IncidentState.investigating, actor_type="agent", actor_id="test"
        )
        if state == IncidentState.hypothesis_formed:
            await repo.record_transition(
                incident, IncidentState.hypothesis_formed, actor_type="agent", actor_id="test"
            )
    await session.commit()
    return incident


async def test_tier1_shadow_mode_executes_nothing(session: AsyncSession):
    """Default posture: shadow ON — the action is recorded, infrastructure untouched."""
    assert get_settings().resolution_shadow_mode is True
    incident = await _incident(session, "checkout-service")
    gateway = StatefulK8sGateway()
    action = await propose_remediation(
        session,
        incident,
        root_cause_category="resource_exhaustion",
        hypothesis="pod is OOM-looping",
        observer_approved=True,
        target_pod="checkout-service-abc-1",
        decision=_decision("restart_pod"),
    )
    assert action is not None and action.tier == 1 and action.shadow is True
    action = await execute_action(session, action, gateway, observer_approved=True)
    assert action.status == RemediationStatus.executed
    assert action.result["shadow"] is True
    assert gateway.writes == []  # nothing touched


async def test_tier1_real_execution_restarts_pod(session: AsyncSession, monkeypatch):
    monkeypatch.setattr(get_settings(), "resolution_shadow_mode", False)
    incident = await _incident(session, "checkout-service")
    gateway = StatefulK8sGateway()
    action = await propose_remediation(
        session,
        incident,
        root_cause_category="resource_exhaustion",
        hypothesis="pod is OOM-looping",
        observer_approved=True,
        target_pod="checkout-service-abc-1",
        decision=_decision("restart_pod"),
    )
    assert action.shadow is False
    action = await execute_action(session, action, gateway, observer_approved=True)
    assert action.status == RemediationStatus.executed
    assert gateway.writes and gateway.writes[0][0] == "restart_pod"
    await session.refresh(incident)
    assert incident.state == IncidentState.monitoring  # executed → monitoring (ESD §6.1)


async def test_kill_switch_blocks_everything(session: AsyncSession, monkeypatch):
    monkeypatch.setattr(get_settings(), "resolution_shadow_mode", False)
    await set_kill_switch(session, engaged=True, actor="test-admin")
    incident = await _incident(session, "checkout-service")
    gateway = StatefulK8sGateway()
    action = await propose_remediation(
        session,
        incident,
        root_cause_category="resource_exhaustion",
        hypothesis="x",
        observer_approved=True,
        target_pod="checkout-service-abc-1",
        decision=_decision("restart_pod"),
    )
    action = await execute_action(session, action, gateway, observer_approved=True)
    assert action.status == RemediationStatus.proposed  # untouched
    assert gateway.writes == []
    await set_kill_switch(session, engaged=False, actor="test-admin")


async def test_tier1_blast_radius_gate_escalates(session: AsyncSession, monkeypatch):
    """FR-8.3: payment-service has a dependent (checkout); with limit 0, no auto-exec."""
    monkeypatch.setattr(get_settings(), "resolution_shadow_mode", False)
    monkeypatch.setattr(get_settings(), "max_blast_radius_dependents", 0)
    incident = await _incident(session, "payment-service")
    gateway = StatefulK8sGateway()
    action = await propose_remediation(
        session,
        incident,
        root_cause_category="resource_exhaustion",
        hypothesis="x",
        observer_approved=True,
        target_pod="payment-service-abc-1",
        decision=_decision("restart_pod"),
    )
    action = await execute_action(session, action, gateway, observer_approved=True)
    assert action.status == RemediationStatus.proposed
    assert gateway.writes == []


async def test_unvalidated_hypothesis_never_auto_executes(session: AsyncSession, monkeypatch):
    monkeypatch.setattr(get_settings(), "resolution_shadow_mode", False)
    incident = await _incident(session, "checkout-service")
    gateway = StatefulK8sGateway()
    action = await propose_remediation(
        session,
        incident,
        root_cause_category="resource_exhaustion",
        hypothesis="x",
        observer_approved=False,
        target_pod="checkout-service-abc-1",
        decision=_decision("restart_pod"),
    )
    action = await execute_action(session, action, gateway, observer_approved=False)
    assert action.status == RemediationStatus.proposed
    assert gateway.writes == []


async def test_tier2_scale_execution_and_compensating_reversal(session: AsyncSession, monkeypatch):
    """The CLAUDE.md §7 requirement: prove the compensating action actually reverses."""
    monkeypatch.setattr(get_settings(), "resolution_shadow_mode", False)
    incident = await _incident(session, "checkout-service")
    gateway = StatefulK8sGateway({"checkout-service": 2})
    action = await propose_remediation(
        session,
        incident,
        root_cause_category="latency_degradation",
        hypothesis="saturation",
        observer_approved=True,
        decision=_decision("scale_deployment", replicas_delta=1),
    )
    assert action.tier == 2
    assert incident.state == IncidentState.remediation_proposed

    # Tier-2 without approval: refused.
    action = await execute_action(session, action, gateway, observer_approved=True)
    assert action.status == RemediationStatus.proposed
    assert gateway.deployments["checkout-service"] == 2

    # Approve (the API route does this in production) and execute: 2 → 3.
    action.status = RemediationStatus.approved
    await IncidentRepository(session).record_transition(
        incident, IncidentState.remediation_approved, actor_type="human", actor_id="test"
    )
    await session.flush()
    action = await execute_action(session, action, gateway, observer_approved=True)
    assert action.status == RemediationStatus.executed
    assert gateway.deployments["checkout-service"] == 3
    assert action.parameters["previous_replicas"] == 2

    # Compensating action restores the original state: 3 → 2.
    result = await execute_compensating_action(session, action, gateway, actor_id=uuid.uuid4())
    assert result["ok"] is True
    assert gateway.deployments["checkout-service"] == 2  # actually reversed
    assert action.status == RemediationStatus.rolled_back


async def test_expired_proposal_cannot_execute(session: AsyncSession, monkeypatch):
    monkeypatch.setattr(get_settings(), "resolution_shadow_mode", False)
    incident = await _incident(session, "checkout-service")
    gateway = StatefulK8sGateway()
    action = await propose_remediation(
        session,
        incident,
        root_cause_category="resource_exhaustion",
        hypothesis="x",
        observer_approved=True,
        target_pod="checkout-service-abc-1",
        decision=_decision("restart_pod"),
    )
    action.expires_at = _now() - datetime.timedelta(minutes=1)
    await session.flush()
    action = await execute_action(session, action, gateway, observer_approved=True)
    assert action.status == RemediationStatus.proposed
    assert gateway.writes == []


async def test_proposal_is_idempotent(session: AsyncSession):
    incident = await _incident(session, "checkout-service")
    kwargs = {
        "root_cause_category": "resource_exhaustion",
        "hypothesis": "x",
        "observer_approved": True,
        "target_pod": "checkout-service-abc-1",
        "decision": _decision("restart_pod"),
    }
    a1 = await propose_remediation(session, incident, **kwargs)
    a2 = await propose_remediation(session, incident, **kwargs)
    assert a1.id == a2.id  # same idempotency key → same row, no duplicate


async def test_no_decision_proposes_nothing(session: AsyncSession):
    """A proposal without recorded reasoning is not a proposal.

    Guards the contract that made `decision` required: acting on infrastructure with no
    justification attached is precisely what the Resolution agent exists to prevent, so an
    omitted decision must produce nothing rather than falling back to a default action.
    """
    incident = await _incident(session, "checkout-service")
    action = await propose_remediation(
        session,
        incident,
        root_cause_category="resource_exhaustion",
        hypothesis="x",
        observer_approved=True,
        target_pod="checkout-service-abc-1",
    )
    assert action is None
