"""Agent execution records: steps, messages, and evidence citations (ESD §6).

These tables are the durable audit trail that makes replay mode possible (PRD FR-9): every agent
step, message, and citation is persisted with the ``incident_id`` correlation key so a resolved
incident can be reconstructed from the database alone, with no live infrastructure.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.enums import AgentMessageType, EvidenceType
from db.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentStep(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One agent invocation, including per-LLM-call cost/latency accounting (PRD FR-8.2)."""

    __tablename__ = "agent_steps"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    # Set for RCA ensemble passes (PRD FR-3.1); NULL for single-pass agents.
    ensemble_pass_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_used: Mapped[str | None] = mapped_column(String, nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    citations: Mapped[list[EvidenceCitation]] = relationship(
        back_populates="agent_step", cascade="all, delete-orphan"
    )


class AgentMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A reasoning/action/handoff message emitted by an agent (ESD §6)."""

    __tablename__ = "agent_messages"

    incident_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    agent_name: Mapped[str] = mapped_column(String, nullable=False)
    message_type: Mapped[AgentMessageType] = mapped_column(
        Enum(AgentMessageType, name="agent_message_type"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)


class EvidenceCitation(UUIDPrimaryKeyMixin, Base):
    """A citation linking an RCA claim to specific evidence (PRD FR-3.2, FR-8.1).

    ``evidence_snippet_redacted`` stores only the PII-redacted snippet (PRD FR-16); the raw text
    never lands here. ``validated_by_observer`` gates whether the claim may be surfaced to a human.
    """

    __tablename__ = "evidence_citations"

    agent_step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_steps.id", ondelete="CASCADE"), nullable=False, index=True
    )
    evidence_type: Mapped[EvidenceType] = mapped_column(
        Enum(EvidenceType, name="evidence_type"), nullable=False
    )
    evidence_ref: Mapped[str] = mapped_column(String, nullable=False)
    evidence_snippet_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_by_observer: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    agent_step: Mapped[AgentStep] = relationship(back_populates="citations")
