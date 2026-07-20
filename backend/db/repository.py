"""Repository layer — the only place raw queries live (ESD §24 Repository pattern).

Agent and API code depends on these methods, never on hand-written SQL scattered through business
logic. All methods are ``async`` (CLAUDE.md §3) and take an :class:`AsyncSession` the caller owns,
so transaction boundaries stay with the unit of work (see ``core.db.session_scope``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.enums import ActorType, AgentMessageType, AlertSource, EvidenceType, IncidentState, Severity
from db.models import (
    AgentMessage,
    AgentStep,
    AuditLog,
    EvidenceCitation,
    Incident,
    IncidentStateTransition,
)
from db.state_machine import assert_legal_transition


class IncidentRepository:
    """Reads and writes for incidents and their transitions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_incident(
        self,
        *,
        external_alert_id: str,
        alert_source: AlertSource,
        title: str,
        service_name: str,
        severity: Severity,
    ) -> tuple[Incident, bool]:
        """Idempotently create-or-return an incident (PRD FR-10.1).

        Uniqueness on ``(alert_source, external_alert_id)`` is enforced by the database, so a
        webhook retry with the same external id can never create a second incident. Returns
        ``(incident, created)`` where ``created`` is False when an existing incident was returned.
        """
        stmt = (
            pg_insert(Incident)
            .values(
                external_alert_id=external_alert_id,
                alert_source=alert_source,
                title=title,
                service_name=service_name,
                severity=severity,
                state=IncidentState.open,
            )
            .on_conflict_do_nothing(constraint="uq_incidents_source_external_id")
            .returning(Incident.id)
        )
        result = await self.session.execute(stmt)
        inserted_id = result.scalar_one_or_none()
        await self.session.flush()

        if inserted_id is not None:
            incident = await self.get(inserted_id)
            assert incident is not None  # just inserted
            return incident, True

        # Conflict: the incident already exists — fetch and return it, no duplicate created.
        existing = await self.get_by_external_id(alert_source, external_alert_id)
        assert existing is not None  # conflict implies a row exists
        return existing, False

    async def get(self, incident_id: uuid.UUID) -> Incident | None:
        """Return an incident by id, or None."""
        return await self.session.get(Incident, incident_id)

    async def get_by_external_id(
        self, alert_source: AlertSource, external_alert_id: str
    ) -> Incident | None:
        """Return the incident for a given source/external-id pair, or None."""
        stmt = select(Incident).where(
            Incident.alert_source == alert_source,
            Incident.external_alert_id == external_alert_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_incidents(
        self,
        *,
        state: IncidentState | None = None,
        service_name: str | None = None,
        severity: Severity | None = None,
        limit: int = 100,
    ) -> list[Incident]:
        """List incidents, newest first, with optional state/service/severity filters (ESD §7)."""
        stmt = select(Incident).order_by(Incident.created_at.desc()).limit(limit)
        if state is not None:
            stmt = stmt.where(Incident.state == state)
        if service_name is not None:
            stmt = stmt.where(Incident.service_name == service_name)
        if severity is not None:
            stmt = stmt.where(Incident.severity == severity)
        return list((await self.session.execute(stmt)).scalars().all())

    async def record_transition(
        self,
        incident: Incident,
        to_state: IncidentState,
        *,
        actor_type: ActorType,
        actor_id: str,
    ) -> IncidentStateTransition:
        """Validate and persist a lifecycle transition (ESD §6.1).

        Raises :class:`db.state_machine.IllegalTransitionError` if the move is not allow-listed, so
        an invalid transition is rejected before any row is written.
        """
        assert_legal_transition(incident.state, to_state)
        transition = IncidentStateTransition(
            incident_id=incident.id,
            from_state=incident.state,
            to_state=to_state,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        self.session.add(transition)
        incident.state = to_state
        await self.session.flush()
        return transition


class AgentRepository:
    """Writes for the agent audit trail: steps, messages, citations (PRD FR-9 replay)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add_step(self, **kwargs: object) -> AgentStep:
        """Persist an :class:`AgentStep` (per-LLM-call accounting, PRD FR-8.2)."""
        step = AgentStep(**kwargs)  # type: ignore[arg-type]
        self.session.add(step)
        await self.session.flush()
        return step

    async def add_message(
        self,
        *,
        incident_id: uuid.UUID,
        agent_name: str,
        message_type: AgentMessageType,
        content: str,
        message_metadata: dict | None = None,
    ) -> AgentMessage:
        """Persist an :class:`AgentMessage`."""
        message = AgentMessage(
            incident_id=incident_id,
            agent_name=agent_name,
            message_type=message_type,
            content=content,
            message_metadata=message_metadata,
        )
        self.session.add(message)
        await self.session.flush()
        return message

    async def add_citation(
        self,
        *,
        agent_step_id: uuid.UUID,
        evidence_type: EvidenceType,
        evidence_ref: str,
        evidence_snippet_redacted: str | None = None,
        validated_by_observer: bool = False,
    ) -> EvidenceCitation:
        """Persist an :class:`EvidenceCitation` (PRD FR-3.2)."""
        citation = EvidenceCitation(
            agent_step_id=agent_step_id,
            evidence_type=evidence_type,
            evidence_ref=evidence_ref,
            evidence_snippet_redacted=evidence_snippet_redacted,
            validated_by_observer=validated_by_observer,
        )
        self.session.add(citation)
        await self.session.flush()
        return citation


class AuditRepository:
    """Append-only writes to the partitioned audit log (ESD §17, CLAUDE.md §17)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def write(
        self,
        *,
        actor_type: ActorType,
        actor_id: str,
        action: str,
        target: str | None = None,
        incident_id: uuid.UUID | None = None,
        audit_metadata: dict | None = None,
    ) -> AuditLog:
        """Append one audit entry, correlated to an incident where applicable."""
        entry = AuditLog(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            target=target,
            incident_id=incident_id,
            audit_metadata=audit_metadata,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry
