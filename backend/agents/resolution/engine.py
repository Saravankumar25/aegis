"""Resolution Agent engine: tiered decide → gate → (shadow-)execute (FR-4, FR-8.3).

Execution passes through four independent gates, in order, every time:
kill switch → tier/approval status → global breaker → resource lease. Any gate failing
leaves an audit trail and NEVER executes. Tier-1 additionally requires an
observer-approved hypothesis and a blast radius within the configured limit (FR-8.3),
and runs in shadow mode by default until an operator turns shadow off.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.resolution.actions import ActionSpec
from agents.resolution.planner import ResolutionDecision
from agents.topology import dependents_of
from api.events import publish_event
from core.config import get_settings
from core.logging import get_logger
from db.enums import ActorType, IncidentState, RemediationStatus
from db.models import Incident, RemediationAction
from db.repository import AuditRepository, IncidentRepository
from safety.circuit_breaker.breaker import (
    effective_tier,
    is_globally_tripped,
    record_tier1_execution,
)
from safety.kill_switch.switch import is_kill_switch_engaged
from safety.resource_lease.lease import acquire_lease, release_lease


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def estimate_blast_radius(service_name: str) -> dict[str, Any]:
    """FR-4.4: dependents that would feel this action, from the topology map."""
    dependents = dependents_of(service_name)
    return {"service": service_name, "dependents": dependents, "count": len(dependents)}


def _format_reasoning(
    *,
    decision: ResolutionDecision,
    root_cause_category: str,
    hypothesis: str,
    spec: ActionSpec,
    target_id: str,
    tier: int,
    observer_approved: bool,
) -> str:
    """Render the approval-facing justification for a proposed action."""
    parts = [
        f"Resolution agent chose {spec.action_type} on {target_id} "
        f"(confidence {decision.confidence:.2f}).",
        f"Why: {decision.reasoning}",
    ]
    if decision.expected_effect:
        parts.append(f"Expected effect: {decision.expected_effect}")
    if decision.alternatives_rejected:
        parts.append("Alternatives rejected: " + "; ".join(decision.alternatives_rejected))
    if decision.clamped:
        # An approver must see that the agent asked for more than it was allowed.
        parts.append("Parameters clamped to catalog bounds: " + "; ".join(decision.clamped))
    parts.append(
        f"Grounded in RCA hypothesis ({root_cause_category}): {hypothesis}. "
        f"Catalog tier {spec.tier}, effective tier {tier} after rate-limit check; "
        f"observer_approved={observer_approved}."
    )
    return "\n".join(parts)


async def propose_remediation(
    session: AsyncSession,
    incident: Incident,
    *,
    root_cause_category: str,
    hypothesis: str,
    observer_approved: bool,
    target_pod: str | None = None,
    decision: ResolutionDecision | None = None,
) -> RemediationAction | None:
    """Create (idempotently) the remediation proposal for a validated hypothesis.

    ``decision`` carries the Resolution agent's LLM choice. It is passed in rather than made
    here so that this function stays a pure persistence-and-safety step: everything below
    determines *tier, expiry, blast radius, shadow status and idempotency*, none of which the
    model participates in. A caller that omits it gets no proposal, because acting without
    recorded reasoning is the failure mode the whole agent exists to prevent.

    Returns None when no action was chosen, when the chosen action needs a target that was
    never identified, or when the identical proposal already exists (the unique idempotency
    key absorbs re-runs).
    """
    if decision is None or decision.spec is None:
        return None
    spec: ActionSpec = decision.spec

    settings = get_settings()
    service = incident.service_name
    if spec.target_resource_type == "pod":
        if not target_pod:
            return None  # nothing concrete to restart
        target_id = f"meridian/pod/{target_pod}"
        parameters: dict[str, Any] = {"name": target_pod}
    else:
        target_id = f"meridian/deployment/{service}"
        parameters = {"name": service}
    # Model-chosen values, already clamped to the catalog's declared bounds by the planner.
    parameters.update(decision.parameters)

    tier = await effective_tier(session, service, spec.tier)
    idempotency_key = f"{incident.id}:{spec.action_type}:{target_id}"

    existing = (
        await session.execute(
            select(RemediationAction).where(RemediationAction.idempotency_key == idempotency_key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    action = RemediationAction(
        incident_id=incident.id,
        tier=tier,
        action_type=spec.action_type,
        target_resource_type=spec.target_resource_type,
        target_resource_id=target_id,
        idempotency_key=idempotency_key,
        parameters=parameters,
        compensating_action=spec.compensating,
        # FR-4.3: full reasoning logged before any execution. The agent's own justification
        # leads, because that is what a human approving this at 3am actually needs to weigh;
        # the deterministic facts (tiering, clamping) follow as provenance. Recording only
        # the machine-generated sentence, as before, told an approver nothing about *why*
        # this action was believed to address this cause.
        reasoning=_format_reasoning(
            decision=decision,
            root_cause_category=root_cause_category,
            hypothesis=hypothesis,
            spec=spec,
            target_id=target_id,
            tier=tier,
            observer_approved=observer_approved,
        ),
        blast_radius=estimate_blast_radius(service),
        status=RemediationStatus.proposed,
        proposed_at=_now(),
        expires_at=_now() + datetime.timedelta(minutes=settings.proposal_expiry_minutes),
        shadow=settings.resolution_shadow_mode and tier == 1,
    )
    session.add(action)
    await session.flush()
    await AuditRepository(session).write(
        actor_type=ActorType.agent,
        actor_id="resolution",
        action="remediation_proposed",
        target=target_id,
        incident_id=incident.id,
        audit_metadata={"action_type": spec.action_type, "tier": tier, "shadow": action.shadow},
    )
    await publish_event(
        session,
        incident.id,
        "remediation_proposed",
        {"action_type": spec.action_type, "tier": tier, "target": target_id},
    )

    # Tier-2/3 proposals move the incident into the approval flow.
    repo = IncidentRepository(session)
    if tier >= 2 and incident.state == IncidentState.hypothesis_formed:
        await repo.record_transition(
            incident,
            IncidentState.remediation_proposed,
            actor_type=ActorType.agent,
            actor_id="resolution",
        )
    return action


class GateRefusal(Exception):
    """An execution gate said no. Recorded, never bypassed."""

    def __init__(self, gate: str, reason: str) -> None:
        super().__init__(f"{gate}: {reason}")
        self.gate = gate
        self.reason = reason


async def _check_gates(
    session: AsyncSession, action: RemediationAction, *, observer_approved: bool
) -> None:
    """All gates for execution; raises GateRefusal on the first failure (FR-8.3)."""
    settings = get_settings()
    if await is_kill_switch_engaged(session):
        raise GateRefusal("kill_switch", "engaged — all autonomous action halted (FR-5.3)")
    if action.expires_at <= _now():
        raise GateRefusal("expiry", "proposal expired; re-investigation required")
    if action.tier == 3:
        raise GateRefusal("tier", "Tier-3 actions are never machine-executed")
    if action.tier == 2 and action.status != RemediationStatus.approved:
        raise GateRefusal("approval", "Tier-2 requires explicit human approval (FR-5.1)")
    if action.tier == 1:
        if not observer_approved:
            raise GateRefusal(
                "observer", "Tier-1 auto-exec requires an observer-validated hypothesis"
            )
        if action.blast_radius.get("count", 0) > settings.max_blast_radius_dependents:
            raise GateRefusal(
                "blast_radius",
                f"{action.blast_radius.get('count')} dependents exceed the Tier-1 limit "
                f"({settings.max_blast_radius_dependents}) — escalate to a human (FR-8.3)",
            )
        if await is_globally_tripped(session):
            raise GateRefusal("global_breaker", "mass-action breaker is tripped (FR-12)")


async def execute_action(
    session: AsyncSession,
    action: RemediationAction,
    gateway: Any,
    *,
    observer_approved: bool,
) -> RemediationAction:
    """Gate-checked (shadow-)execution of one remediation action.

    Idempotent: an action already past ``approved``/``leased`` state is returned as-is.
    The resource lease is held only around the actual execution and always released.
    """
    log = get_logger(incident_id=str(action.incident_id), component="resolution")
    if action.status in (RemediationStatus.executed, RemediationStatus.failed):
        return action

    audit = AuditRepository(session)
    try:
        await _check_gates(session, action, observer_approved=observer_approved)
    except GateRefusal as refusal:
        await audit.write(
            actor_type=ActorType.agent,
            actor_id="resolution",
            action="execution_refused",
            target=action.target_resource_id,
            incident_id=action.incident_id,
            audit_metadata={"gate": refusal.gate, "reason": refusal.reason},
        )
        await publish_event(
            session,
            action.incident_id,
            "execution_refused",
            {"gate": refusal.gate, "reason": refusal.reason},
        )
        log.warning("execution_refused", gate=refusal.gate, reason=refusal.reason)
        return action

    # Shadow mode: record exactly what would have happened, touch nothing (Tier-1 ramp-up).
    if action.shadow:
        action.status = RemediationStatus.executed
        action.executed_at = _now()
        action.result = {
            "shadow": True,
            "would_call": {
                "server": "k8s",
                "tool": action.action_type,
                "params": action.parameters,
            },
        }
        await audit.write(
            actor_type=ActorType.agent,
            actor_id="resolution",
            action="remediation_shadow_executed",
            target=action.target_resource_id,
            incident_id=action.incident_id,
            audit_metadata=action.result,
        )
        await publish_event(
            session, action.incident_id, "remediation_shadow_executed", action.result
        )
        await session.flush()
        return action

    lease = await acquire_lease(
        session,
        target_resource_type=action.target_resource_type,
        target_resource_id=action.target_resource_id,
        remediation_action_id=action.id,
    )
    if lease is None:
        await audit.write(
            actor_type=ActorType.agent,
            actor_id="resolution",
            action="execution_refused",
            target=action.target_resource_id,
            incident_id=action.incident_id,
            audit_metadata={"gate": "resource_lease", "reason": "target already leased"},
        )
        return action
    action.status = RemediationStatus.leased
    await session.flush()

    try:
        params = {**action.parameters, "idempotency_key": action.idempotency_key}
        if action.action_type == "scale_deployment" and "replicas" not in params:
            # Resolve the concrete replica target at execution time: current + delta.
            deployments = await gateway.call("k8s", "list_deployments", {})
            current = None
            if deployments.get("ok"):
                for d in deployments["data"]:
                    if d["name"] == action.parameters["name"]:
                        current = d["replicas_desired"]
            if current is None:
                raise RuntimeError("could not resolve current replica count")
            params["replicas"] = current + params.pop("replicas_delta", 1)
            action.parameters = {
                **action.parameters,
                "previous_replicas": current,
                "replicas": params["replicas"],
            }

        result = await gateway.call("k8s", action.action_type, params)
        if result.get("ok"):
            action.status = RemediationStatus.executed
            action.executed_at = _now()
            action.result = result.get("data")
            if action.tier == 1:
                await record_tier1_execution(session)
            await audit.write(
                actor_type=ActorType.agent,
                actor_id="resolution",
                action="remediation_executed",
                target=action.target_resource_id,
                incident_id=action.incident_id,
                audit_metadata={"action_type": action.action_type, "result": action.result},
            )
            await publish_event(
                session,
                action.incident_id,
                "remediation_executed",
                {"action_type": action.action_type, "target": action.target_resource_id},
            )
        else:
            # Failed: compensating action is OFFERED to the human, never auto-fired (ESD §12).
            action.status = RemediationStatus.failed
            action.result = {"error": result.get("error"), "error_kind": result.get("error_kind")}
            await audit.write(
                actor_type=ActorType.agent,
                actor_id="resolution",
                action="remediation_failed",
                target=action.target_resource_id,
                incident_id=action.incident_id,
                audit_metadata={
                    "error": result.get("error"),
                    "compensating_action_offered": action.compensating_action,
                },
            )
            await publish_event(
                session,
                action.incident_id,
                "remediation_failed",
                {"error": result.get("error"), "compensating_action": action.compensating_action},
            )
    finally:
        await release_lease(session, lease.id)
    await session.flush()

    # Successful real execution advances the incident lifecycle (ESD §6.1).
    if action.status == RemediationStatus.executed:
        repo = IncidentRepository(session)
        incident = await repo.get(action.incident_id)
        if incident is not None:
            if incident.state in (
                IncidentState.hypothesis_formed,
                IncidentState.remediation_approved,
            ):
                await repo.record_transition(
                    incident,
                    IncidentState.remediation_executed,
                    actor_type=ActorType.agent,
                    actor_id="resolution",
                )
                await repo.record_transition(
                    incident,
                    IncidentState.monitoring,
                    actor_type=ActorType.agent,
                    actor_id="resolution",
                )
    return action


async def execute_compensating_action(
    session: AsyncSession, action: RemediationAction, gateway: Any, *, actor_id: uuid.UUID
) -> dict[str, Any]:
    """Human-triggered rollback of an executed scale action (the documented undo)."""
    if action.action_type != "scale_deployment":
        return {"ok": False, "error": "no machine-executable compensating action"}
    previous = action.parameters.get("previous_replicas")
    if previous is None:
        return {"ok": False, "error": "previous replica count was not recorded"}
    result = await gateway.call(
        "k8s",
        "scale_deployment",
        {
            "name": action.parameters["name"],
            "replicas": previous,
            "idempotency_key": f"{action.idempotency_key}:compensate",
        },
    )
    if result.get("ok"):
        action.status = RemediationStatus.rolled_back
        await AuditRepository(session).write(
            actor_type=ActorType.human,
            actor_id=str(actor_id),
            action="remediation_rolled_back",
            target=action.target_resource_id,
            incident_id=action.incident_id,
            audit_metadata={"restored_replicas": previous},
        )
        await session.flush()
    return result
