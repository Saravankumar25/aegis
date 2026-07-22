"""Incident routes: ingestion webhook, list/detail, SSE stream, replay (ESD §7, §9)."""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.communication.writer import post_update
from agents.triage.classifier import classify_severity
from api.deps import get_current_user, get_llm_provider, get_session, require_role
from api.events import hub, publish_event
from api.schemas import (
    AgentMessageOut,
    AgentStepOut,
    AlertIn,
    IncidentDetailOut,
    IncidentOut,
    IngestResult,
    ReplayEventOut,
    ReplayOut,
    TransitionOut,
)
from core.config import get_settings
from db.enums import ActorType, IncidentState, Severity, UserRole
from db.models import AgentMessage, AgentStep, Incident, IncidentStateTransition, User
from db.repository import AuditRepository, IncidentRepository
from db.state_machine import IllegalTransitionError
from memory.store import draft_summary
from providers.base import LLMProvider

router = APIRouter(prefix="/incidents", tags=["incidents"])

_ACTIVE_STATES = (
    IncidentState.open,
    IncidentState.investigating,
    IncidentState.hypothesis_formed,
    # An escalated incident is still open — it is waiting on a human. A second alert for the
    # same service must merge into it rather than open a duplicate, or escalating would turn
    # one incident into a stream of new ones for as long as the fault persists.
    IncidentState.escalated,
    IncidentState.reopened,
)


async def _find_dedup_target(
    session: AsyncSession, alert: AlertIn, window_seconds: int
) -> Incident | None:
    """FR-1.2: an active incident for the same service+source within the window is the
    same underlying incident; a second alert merges instead of opening a new one."""
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(seconds=window_seconds)
    stmt = (
        select(Incident)
        .where(
            Incident.service_name == alert.service_name,
            Incident.alert_source == alert.alert_source,
            Incident.state.in_(_ACTIVE_STATES),
            Incident.created_at >= cutoff,
        )
        .order_by(Incident.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


@router.post("", response_model=IngestResult, status_code=201)
async def ingest_alert(
    alert: AlertIn, session: AsyncSession = Depends(get_session)
) -> IngestResult:
    """Alert ingestion webhook (FR-1.1). Idempotent (FR-10.1) and deduplicating (FR-1.2)."""
    repo = IncidentRepository(session)
    audit = AuditRepository(session)

    # Exact retry of the same alert id: the unique constraint makes this a no-op.
    existing = await repo.get_by_external_id(alert.alert_source, alert.external_alert_id)
    if existing is not None:
        return IngestResult(
            incident_id=existing.id,
            created=False,
            deduplicated=False,
            severity=existing.severity,
            state=existing.state,
        )

    # Different alert id, same underlying incident within the dedup window: merge.
    target = await _find_dedup_target(session, alert, get_settings().dedup_window_seconds)
    if target is not None:
        await audit.write(
            actor_type=ActorType.system,
            actor_id="ingestion",
            action="alert_merged",
            target=f"incident/{target.id}",
            incident_id=target.id,
            audit_metadata={
                "external_alert_id": alert.external_alert_id,
                "kind": alert.kind,
                "title": alert.title,
            },
        )
        await publish_event(session, target.id, "alert_merged", {"title": alert.title})
        return IngestResult(
            incident_id=target.id,
            created=False,
            deduplicated=True,
            severity=target.severity,
            state=target.state,
        )

    severity = classify_severity(alert.service_name, alert.kind)
    incident, created = await repo.upsert_incident(
        external_alert_id=alert.external_alert_id,
        alert_source=alert.alert_source,
        title=alert.title,
        service_name=alert.service_name,
        severity=severity,
        alert_kind=alert.kind,
        alert_value=alert.value,
    )
    if created:
        await audit.write(
            actor_type=ActorType.system,
            actor_id="ingestion",
            action="incident_created",
            target=f"incident/{incident.id}",
            incident_id=incident.id,
            audit_metadata={"severity": severity, "kind": alert.kind},
        )
        # The NOTIFY doubles as the worker's wake-up call: enqueue == commit the open row.
        await publish_event(
            session, incident.id, "incident_created", {"service": alert.service_name}
        )
    return IngestResult(
        incident_id=incident.id,
        created=created,
        deduplicated=False,
        severity=incident.severity,
        state=incident.state,
    )


@router.get("", response_model=list[IncidentOut])
async def list_incidents(
    state: IncidentState | None = None,
    service_name: str | None = None,
    severity: Severity | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[Incident]:
    """List incidents, filterable by state/service/severity (ESD §7)."""
    return await IncidentRepository(session).list_incidents(
        state=state, service_name=service_name, severity=severity, limit=min(limit, 500)
    )


async def _load_detail(session: AsyncSession, incident_id: uuid.UUID) -> IncidentDetailOut:
    incident = await IncidentRepository(session).get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    transitions = (
        (
            await session.execute(
                select(IncidentStateTransition)
                .where(IncidentStateTransition.incident_id == incident_id)
                .order_by(IncidentStateTransition.created_at)
            )
        )
        .scalars()
        .all()
    )
    steps = (
        (
            await session.execute(
                select(AgentStep)
                .where(AgentStep.incident_id == incident_id)
                .order_by(AgentStep.created_at)
            )
        )
        .scalars()
        .all()
    )
    for step in steps:
        await session.refresh(step, ["citations"])
    messages = (
        (
            await session.execute(
                select(AgentMessage)
                .where(AgentMessage.incident_id == incident_id)
                .order_by(AgentMessage.created_at)
            )
        )
        .scalars()
        .all()
    )
    return IncidentDetailOut(
        **IncidentOut.model_validate(incident).model_dump(),
        transitions=[TransitionOut.model_validate(t) for t in transitions],
        steps=[AgentStepOut.model_validate(s) for s in steps],
        messages=[AgentMessageOut.model_validate(m) for m in messages],
    )


@router.get("/stream")
async def all_incidents_stream(
    request: Request,
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """SSE stream of events across all incidents (dashboard list view, ESD §7)."""

    async def stream() -> AsyncIterator[str]:
        queue = hub.subscribe("*")
        try:
            yield ": connected\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {event['event']}\ndata: {json.dumps(event)}\n\n"
        finally:
            hub.unsubscribe("*", queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{incident_id}", response_model=IncidentDetailOut)
async def incident_detail(
    incident_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> IncidentDetailOut:
    """Full incident detail (ESD §7)."""
    return await _load_detail(session, incident_id)


@router.get("/{incident_id}/stream")
async def incident_stream(
    incident_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """SSE stream of live updates for one incident (ESD §7; no polling, CLAUDE.md §11)."""
    incident = await IncidentRepository(session).get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")

    async def stream() -> AsyncIterator[str]:
        queue = hub.subscribe(str(incident_id))
        try:
            snapshot = IncidentOut.model_validate(incident).model_dump(mode="json")
            yield f"event: snapshot\ndata: {json.dumps(snapshot)}\n\n"
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"  # comment frame keeps proxies from closing us
                    continue
                yield f"event: {event['event']}\ndata: {json.dumps(event)}\n\n"
        finally:
            hub.unsubscribe(str(incident_id), queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{incident_id}/resolve", response_model=IncidentOut)
async def resolve_incident(
    incident_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.on_call_engineer, UserRole.admin)),
    provider: LLMProvider = Depends(get_llm_provider),
) -> Incident:
    """Human resolution (V1.5, ESD §7): closes the loop and drafts the memory summary.

    Legal from hypothesis_formed / monitoring / remediation_proposed (the FR-5.2
    rejected-and-fixed-manually path); the state machine rejects anything else.
    """
    repo = IncidentRepository(session)
    incident = await repo.get(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="incident not found")
    try:
        await repo.record_transition(
            incident,
            IncidentState.resolved,
            actor_type=ActorType.human,
            actor_id=str(user.id),
        )
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    incident.resolved_at = datetime.datetime.now(datetime.UTC)

    # FR-7.1: draft the memory summary from the final validated RCA step (pending approval).
    last_rca = (
        await session.execute(
            select(AgentStep)
            .where(
                AgentStep.incident_id == incident_id,
                AgentStep.agent_name == "rca",
                AgentStep.ensemble_pass_index.is_(None),
            )
            .order_by(AgentStep.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    output = last_rca.structured_output if last_rca and last_rca.structured_output else {}
    await draft_summary(
        session,
        incident,
        root_cause_category=str(output.get("root_cause_category", "unknown")),
        hypothesis=str(output.get("hypothesis", "resolved without automated hypothesis")),
        # The Memory agent writes the lesson here rather than in the worker: resolution is a
        # human action taken through the API, and the summary must exist by the time this
        # request returns so it appears in the approval queue immediately.
        provider=provider,
    )
    await post_update(
        session,
        None,  # Slack mirroring runs in the worker; API-side updates are dashboard-only
        incident_id=incident_id,
        phase="resolved",
        service=incident.service_name,
        severity=str(incident.severity),
    )
    await publish_event(session, incident_id, "state_changed", {"state": "resolved"})
    # updated_at is server-generated (onupdate); refresh so serialization needs no lazy IO.
    await session.flush()
    await session.refresh(incident)
    return incident


@router.get("/{incident_id}/replay", response_model=ReplayOut)
async def incident_replay(
    incident_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> ReplayOut:
    """Ordered agent-decision history, reconstructed from persisted rows only (FR-9)."""
    detail = await _load_detail(session, incident_id)
    events: list[tuple[datetime.datetime, str, str | None, str, dict]] = []
    for t in detail.transitions:
        events.append(
            (
                t.created_at,
                "transition",
                None,
                f"{t.from_state} → {t.to_state} ({t.actor_type}:{t.actor_id})",
                t.model_dump(mode="json"),
            )
        )
    for s in detail.steps:
        label = f"{s.agent_name} step"
        if s.ensemble_pass_index is not None:
            label += f" (ensemble pass {s.ensemble_pass_index})"
        events.append((s.created_at, "step", s.agent_name, label, s.model_dump(mode="json")))
    for m in detail.messages:
        events.append(
            (
                m.created_at,
                "message",
                m.agent_name,
                m.content[:160],
                m.model_dump(mode="json"),
            )
        )
    events.sort(key=lambda e: e[0])
    return ReplayOut(
        incident=IncidentOut(**detail.model_dump(exclude={"transitions", "steps", "messages"})),
        events=[
            ReplayEventOut(
                sequence=i, at=at, kind=kind, agent_name=agent, summary=summary, detail=payload
            )
            for i, (at, kind, agent, summary, payload) in enumerate(events)
        ],
    )
