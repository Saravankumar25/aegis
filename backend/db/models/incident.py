"""Incident and incident-state-transition models (ESD §6, §6.1).

An incident is the single correlation anchor for an investigation. Idempotent ingestion (PRD FR-10)
is enforced at the database level by the ``UNIQUE(alert_source, external_alert_id)`` constraint, so
a webhook retry can never create a second incident row.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.enums import ActorType, AlertSource, IncidentState, Severity
from db.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Incident(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A single incident under investigation (ESD §6)."""

    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint(
            "alert_source", "external_alert_id", name="uq_incidents_source_external_id"
        ),
    )

    external_alert_id: Mapped[str] = mapped_column(String, nullable=False)
    alert_source: Mapped[AlertSource] = mapped_column(
        Enum(AlertSource, name="alert_source"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)
    service_name: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[Severity] = mapped_column(Enum(Severity, name="severity"), nullable=False)
    state: Mapped[IncidentState] = mapped_column(
        Enum(IncidentState, name="incident_state"),
        nullable=False,
        default=IncidentState.open,
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    transitions: Mapped[list[IncidentStateTransition]] = relationship(
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentStateTransition.created_at",
    )


class IncidentStateTransition(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An audited lifecycle transition, validated against the allow-list (ESD §6.1)."""

    __tablename__ = "incident_state_transitions"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_state: Mapped[IncidentState] = mapped_column(
        Enum(IncidentState, name="incident_state"), nullable=False
    )
    to_state: Mapped[IncidentState] = mapped_column(
        Enum(IncidentState, name="incident_state"), nullable=False
    )
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String, nullable=False)

    incident: Mapped[Incident] = relationship(back_populates="transitions")
