"""V1.5 routes: approval workflow, circuit-breaker status/clear, kill switch (ESD §7, §8).

Every state-changing endpoint enforces its role SERVER-SIDE (ESD §8); the frontend's
gating is cosmetic. Approval applies the decision only — execution stays in the worker
(ESD §11: the API never acts on infrastructure inline).
"""

from __future__ import annotations

import datetime
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_session, require_role
from api.errors import AegisError
from api.events import publish_event
from api.schemas import (
    ApprovalIn,
    BreakerStatusOut,
    KillSwitchIn,
    PendingApprovalOut,
    RemediationActionOut,
)
from db.enums import ActorType, ApprovalDecision, IncidentState, RemediationStatus, UserRole
from db.models import ActionCircuitBreakerEvent, Approval, Incident, RemediationAction, User
from db.repository import AuditRepository, IncidentRepository
from safety.circuit_breaker.breaker import clear_global_breaker, is_globally_tripped
from safety.kill_switch.switch import is_kill_switch_engaged, set_kill_switch

router = APIRouter(tags=["actions"])


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


@router.get("/approvals", response_model=list[PendingApprovalOut])
async def pending_approvals(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.on_call_engineer, UserRole.admin, UserRole.viewer)),
) -> list[PendingApprovalOut]:
    """The Tier-2 approval queue: proposed, unexpired actions (viewers may look)."""
    rows = (
        await session.execute(
            select(RemediationAction, Incident)
            .join(Incident, Incident.id == RemediationAction.incident_id)
            .where(
                RemediationAction.status == RemediationStatus.proposed,
                RemediationAction.tier >= 2,
                RemediationAction.expires_at > _now(),
            )
            .order_by(RemediationAction.proposed_at.desc())
        )
    ).all()
    return [
        PendingApprovalOut(
            action=RemediationActionOut.model_validate(action),
            incident_title=incident.title,
            service_name=incident.service_name,
            severity=incident.severity,
        )
        for action, incident in rows
    ]


@router.post("/incidents/{incident_id}/approvals", response_model=RemediationActionOut)
async def decide_approval(
    incident_id: uuid.UUID,
    body: ApprovalIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.on_call_engineer, UserRole.admin)),
) -> RemediationAction:
    """Approve or reject a Tier-2 proposal (FR-5.1). Server-side role check (ESD §8)."""
    action = await session.get(RemediationAction, body.action_id, with_for_update=True)
    if action is None or action.incident_id != incident_id:
        raise AegisError("not_found", "no such proposal for this incident", status_code=404)
    if action.status != RemediationStatus.proposed:
        raise AegisError(
            "invalid_state",
            f"proposal is {action.status}, not pending",
            status_code=409,
            incident_id=incident_id,
        )
    if action.expires_at <= _now():
        raise AegisError(
            "proposal_expired",
            "this proposal expired; the incident needs re-investigation",
            status_code=409,
            incident_id=incident_id,
        )
    if action.tier == 3 and body.decision == "approved":
        # A Tier-3 approval records intent but NEVER schedules machine execution.
        raise AegisError(
            "tier3_manual_only",
            "Tier-3 actions must be executed by a human outside Aegis",
            status_code=409,
            incident_id=incident_id,
        )

    decision = ApprovalDecision(body.decision)
    session.add(
        Approval(
            remediation_action_id=action.id,
            approver_user_id=user.id,
            decision=decision,
            decided_at=_now(),
            notes=body.notes,
        )
    )
    incidents = IncidentRepository(session)
    incident = await incidents.get(incident_id)
    if decision == ApprovalDecision.approved:
        action.status = RemediationStatus.approved
        action.approved_by = user.id
        if incident is not None and incident.state == IncidentState.remediation_proposed:
            await incidents.record_transition(
                incident,
                IncidentState.remediation_approved,
                actor_type=ActorType.human,
                actor_id=str(user.id),
            )
    else:
        # FR-5.2: rejected proposals are never auto-retried; flagged for manual work.
        action.status = RemediationStatus.rejected
    await AuditRepository(session).write(
        actor_type=ActorType.human,
        actor_id=str(user.id),
        action=f"proposal_{decision}",
        target=action.target_resource_id,
        incident_id=incident_id,
        audit_metadata={"action_type": action.action_type, "notes": body.notes},
    )
    await publish_event(
        session,
        incident_id,
        "approval_decided",
        {"decision": str(decision), "action_type": action.action_type},
    )
    await session.flush()
    return action


@router.get("/circuit-breaker/status", response_model=BreakerStatusOut)
async def breaker_status(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.viewer, UserRole.on_call_engineer, UserRole.admin)),
) -> BreakerStatusOut:
    """Current state of the global mass-action breaker (ESD §7)."""
    open_trips = (
        await session.execute(
            select(func.count())
            .select_from(ActionCircuitBreakerEvent)
            .where(
                ActionCircuitBreakerEvent.tripped_at.is_not(None),
                ActionCircuitBreakerEvent.cleared_at.is_(None),
            )
        )
    ).scalar_one()
    windows = (
        await session.execute(select(func.count()).select_from(ActionCircuitBreakerEvent))
    ).scalar_one()
    return BreakerStatusOut(tripped=open_trips > 0, open_trips=open_trips, window_count=windows)


@router.post("/circuit-breaker/clear", response_model=BreakerStatusOut)
async def breaker_clear(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.admin)),
) -> BreakerStatusOut:
    """Manually clear a tripped breaker — admin only (ESD §7/§8)."""
    cleared = await clear_global_breaker(session, cleared_by=user.id)
    await AuditRepository(session).write(
        actor_type=ActorType.human,
        actor_id=str(user.id),
        action="circuit_breaker_cleared",
        audit_metadata={"cleared": cleared},
    )
    return BreakerStatusOut(
        tripped=await is_globally_tripped(session), open_trips=0, window_count=cleared
    )


@router.get("/kill-switch")
async def kill_switch_status(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(get_current_user),
) -> dict:
    """Current kill-switch state (FR-5.3).

    Added because the UI had no way to read it: only POST existed, so the safety page
    rendered a hardcoded "Disengaged" that was a *guess*, not a reading. Displaying a
    fabricated safety state is worse than displaying none — an operator checking whether
    autonomy is halted would have been told it was running when it may not have been.

    Readable by any authenticated user, including `viewer`. Knowing whether autonomy is
    halted is not a privileged fact; changing it is, and that stays on the POST.
    """
    return {"engaged": await is_kill_switch_engaged(session)}


@router.post("/kill-switch")
async def kill_switch(
    body: KillSwitchIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.on_call_engineer, UserRole.admin)),
) -> dict:
    """Engage/disengage the kill switch (FR-5.3). Disengaging is admin-only."""
    if not body.engaged and user.role != UserRole.admin:
        raise AegisError(
            "forbidden", "only an admin may disengage the kill switch", status_code=403
        )
    value = await set_kill_switch(session, engaged=body.engaged, actor=user.email)
    await AuditRepository(session).write(
        actor_type=ActorType.human,
        actor_id=str(user.id),
        action="kill_switch_engaged" if body.engaged else "kill_switch_disengaged",
    )
    return {"kill_switch": value, "engaged": await is_kill_switch_engaged(session)}
