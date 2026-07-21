"""V1.5 models: remediation actions, leases, breaker events, approvals, memory, flags.

Every table here is safety-load-bearing (CLAUDE.md §2):
- ``remediation_actions.idempotency_key`` (unique) makes execution retry-safe.
- ``resource_leases`` enforces one active lease per target via a partial unique index —
  in the database, not application code (ESD §6).
- ``action_circuit_breaker_events`` records global-breaker windows and trips (ESD §17).
- ``system_flags`` holds the kill switch state durably (survives every process restart).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.enums import ApprovalDecision, RemediationStatus
from db.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RemediationAction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One proposed/executed remediation with its pre-registered compensating action.

    There is no such thing as a remediation action without a documented undo
    (CLAUDE.md §17): ``compensating_action`` is NOT NULL by design.
    """

    __tablename__ = "remediation_actions"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 1|2|3 (FR-4.1)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_resource_id: Mapped[str] = mapped_column(String(256), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    compensating_action: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)  # FR-4.3: logged pre-exec
    blast_radius: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)  # FR-4.4
    status: Mapped[RemediationStatus] = mapped_column(
        Enum(RemediationStatus, name="remediation_status"),
        nullable=False,
        default=RemediationStatus.proposed,
    )
    proposed_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    executed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    shadow: Mapped[bool] = mapped_column(nullable=False, default=False)  # Tier-1 shadow mode
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ResourceLease(UUIDPrimaryKeyMixin, Base):
    """Active lease on one infrastructure target (ESD §6, distributed lease pattern)."""

    __tablename__ = "resource_leases"
    __table_args__ = (
        # Exactly one ACTIVE lease per resource, enforced by the database.
        Index(
            "uq_resource_leases_active",
            "target_resource_type",
            "target_resource_id",
            unique=True,
            postgresql_where="released_at IS NULL",
        ),
    )

    target_resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_resource_id: Mapped[str] = mapped_column(String(256), nullable=False)
    held_by_remediation_action_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("remediation_actions.id", ondelete="CASCADE"), nullable=False
    )
    acquired_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ActionCircuitBreakerEvent(UUIDPrimaryKeyMixin, Base):
    """Global mass-action breaker state (ESD §6, §17)."""

    __tablename__ = "action_circuit_breaker_events"

    window_start: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tier1_execution_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tripped_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cleared_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    cleared_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Approval(UUIDPrimaryKeyMixin, Base):
    """A human decision on a Tier-2 proposal (ESD §6)."""

    __tablename__ = "approvals"

    remediation_action_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("remediation_actions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    approver_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    decision: Mapped[ApprovalDecision] = mapped_column(
        Enum(ApprovalDecision, name="approval_decision"), nullable=False
    )
    decided_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class MemorySummary(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Human-approved long-term memory of a resolved incident (ESD §6, FR-7)."""

    __tablename__ = "memory_summaries"
    __table_args__ = (
        # FR-7.3 compound scoping key: retrieval is always (service, incident_type).
        Index("ix_memory_summaries_scope", "service_name", "incident_type"),
    )

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    service_name: Mapped[str] = mapped_column(String(128), nullable=False)
    incident_type: Mapped[str] = mapped_column(String(64), nullable=False)
    symptom: Mapped[str] = mapped_column(Text, nullable=False)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False)
    fix: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class SystemFlag(Base):
    """Durable system-wide flags; row 'kill_switch' is THE kill switch (FR-5.3)."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
