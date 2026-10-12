"""The human decision surface.

An approval request must carry everything needed to decide without
reconstructing the investigation: the incident, the diagnosis, the exact
proposed change, the blast radius, the rollback plan, the verification criteria
and the evidence behind all of it. That is the whole point of the queue - a
human who has to go and find the context will approve on vibes instead.

Deciding is deliberately NOT the same as executing. A granted approval enqueues
work; the worker then re-runs the entire gate chain, because the world may have
moved since the operator looked at it. An approval is permission, not a
command.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from aegis.api.deps import (
    ContainerDep,
    RequireApprover,
    RequireViewer,
    environment_allowed,
    require_environment,
)
from aegis.core.errors import DomainError, NotFoundError
from aegis.core.logging import get_logger
from aegis.persistence.audit import AuditEvent
from aegis.persistence.jobs import JobQueue
from aegis.policy.tiers import profile_for

log = get_logger(__name__)
router = APIRouter(prefix="/approvals", tags=["governance"])


class DecisionRequest(BaseModel):
    decision: Annotated[str, Field(pattern="^(approved|rejected|more_evidence)$")]
    note: Annotated[str, Field(default="", max_length=2000)]


@router.get("", summary="Open approval requests with full decision context")
async def pending(
    principal: RequireViewer,
    container: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    rows = await container.approvals.pending(limit=limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        if not environment_allowed(principal, str(row["environment"])):
            continue
        profile = profile_for_safe(row["action_type"])
        items.append(
            {
                "approval_id": row["approval_id"],
                "action_id": row["action_id"],
                "incident_id": row["incident_id"],
                "requested_at": row["requested_at"].isoformat(),
                "expires_at": row["expires_at"].isoformat(),
                "action": {
                    "type": row["action_type"],
                    "risk_tier": profile,
                    "resource_type": row["resource_type"],
                    "resource_id": row["resource_id"],
                    "service_id": row["service_id"],
                    "environment": row["environment"],
                    "reason": row["reason"],
                },
                "incident": {
                    "title": row["incident_title"],
                    "severity": row["incident_severity"],
                    "confidence": row["incident_confidence"],
                },
                "diagnosis": {
                    "statement": row["diagnosis_statement"],
                    "confidence": row["diagnosis_confidence"],
                },
                "blast_radius": row["blast_radius"],
                "rollback_plan": row["rollback_plan"],
                "verification_plan": row["verification_plan"],
                "expected_effect": row["expected_effect"],
                "supporting_evidence": list(row["supporting_evidence"] or []),
            }
        )
    return {"items": items, "count": len(items)}


def profile_for_safe(action_type: str) -> int:
    """Risk tier for a stored action-type string.

    An unrecognised value reports tier 3 rather than raising: an approval queue
    that 500s because one row holds a retired action type is worse than one
    that shows the safest possible classification.
    """
    from aegis.domain.enums import ActionType

    try:
        return int(profile_for(ActionType(action_type)).tier)
    except ValueError:
        log.warning("unknown action type in approvals queue", action_type=action_type)
        return 3


@router.get("/{approval_id}", summary="One approval request")
async def get_approval(
    approval_id: str, principal: RequireViewer, container: ContainerDep
) -> dict[str, Any]:
    approval = await container.approvals.get(approval_id)
    if approval is None:
        raise NotFoundError("approval not found", context={"approval_id": approval_id})
    incident = await container.incidents.get(approval.incident_id)
    require_environment(principal, incident.environment)
    return {
        "id": approval.id,
        "action_id": approval.action_id,
        "incident_id": approval.incident_id,
        "requested_at": approval.requested_at.isoformat(),
        "expires_at": approval.expires_at.isoformat(),
        "decision": approval.decision,
        "decided_by": approval.decided_by,
        "decided_at": approval.decided_at.isoformat() if approval.decided_at else None,
        "note": approval.note,
    }


@router.post("/{approval_id}/decide", summary="Record a human decision")
async def decide(
    approval_id: str,
    body: DecisionRequest,
    principal: RequireApprover,
    container: ContainerDep,
) -> dict[str, Any]:
    """Record the decision, then enqueue work - never execute inline.

    Two reasons this does not call the executor directly. A remediation can take
    minutes, and holding an HTTP request open for it would time out and leave
    the operator unsure whether it ran. More importantly, the worker re-runs the
    full gate chain: the approval authorises the action, it does not bypass the
    checks that were never about authorisation in the first place.
    """
    existing = await container.approvals.get(approval_id)
    if existing is None:
        raise NotFoundError("approval not found", context={"approval_id": approval_id})
    incident = await container.incidents.get(existing.incident_id)
    require_environment(principal, incident.environment)

    async with container.db.transaction() as conn:
        now = container.approvals.current_time()
        row = await conn.fetchrow(
            """
            UPDATE approvals
               SET decision=$2, decided_by=$3, decided_at=$4, note=$5
             WHERE id=$1 AND decision IS NULL AND expires_at > $4
            RETURNING id, action_id, incident_id, decision
            """,
            approval_id, body.decision, principal.uid, now, body.note,
        )
        changed = row is not None
        if row is None:
            row = await conn.fetchrow(
                "SELECT id, action_id, incident_id, decision, decided_by "
                "FROM approvals WHERE id=$1 FOR UPDATE",
                approval_id,
            )
            if row is None:
                raise NotFoundError("approval not found", context={"approval_id": approval_id})
            if row["decision"] != body.decision:
                raise DomainError(
                    "approval was already decided differently or has expired",
                    context={"approval_id": approval_id, "decision": row["decision"]},
                )

        if changed:
            event = {
                "approved": AuditEvent.APPROVAL_GRANTED,
                "rejected": AuditEvent.APPROVAL_REJECTED,
                "more_evidence": AuditEvent.APPROVAL_MORE_EVIDENCE,
            }[body.decision]
            await conn.execute(
                """
                INSERT INTO audit_log (actor, actor_type, event_type, incident_id,
                                       resource_type, resource_id, detail)
                VALUES ($1, 'human', $2, $3, 'action', $4, $5)
                """,
                principal.uid, event, row["incident_id"], row["action_id"],
                {"approval_id": approval_id, "note": body.note[:1000]},
            )

        enqueued = False
        if changed:
            queue = JobQueue(container.db, container.settings.workflow_dispatch_backend)
            if body.decision == "approved":
                job_id = await queue.enqueue(
                    kind="execute_action",
                    incident_id=str(row["incident_id"]),
                    payload={"action_id": str(row["action_id"]), "approval_id": approval_id},
                    conn=conn,
                )
            elif body.decision == "rejected":
                job_id = await queue.enqueue(
                    kind="horizon_resume",
                    incident_id=str(row["incident_id"]),
                    payload={
                        "action_id": str(row["action_id"]),
                        "approval_id": approval_id,
                        "approved": False,
                    },
                    conn=conn,
                )
            else:
                job_id = await queue.enqueue(
                    kind="investigate",
                    incident_id=str(row["incident_id"]),
                    payload={"reason": "approver requested more evidence"},
                    conn=conn,
                )
            if job_id is None:
                raise DomainError(
                    "approval decision could not be queued",
                    context={"approval_id": approval_id},
                )
            enqueued = job_id is not None

    approval = await container.approvals.get(approval_id)
    if approval is None:
        raise NotFoundError("approval not found", context={"approval_id": approval_id})

    log.info(
        "approval decided",
        approval_id=approval_id,
        decision=approval.decision,
        decided_by=principal.uid,
        work_enqueued=enqueued,
    )
    return {
        "id": approval.id,
        "action_id": approval.action_id,
        "decision": approval.decision,
        "decided_by": approval.decided_by,
        "work_enqueued": enqueued,
    }
