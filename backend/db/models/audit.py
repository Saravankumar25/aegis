"""Audit log model (ESD §6, §17 [review fix]).

The audit log is the fastest-growing table in the system — one row per LLM call, tool call, and
state transition — so it is partitioned by month with a 13-month rolling retention (ESD §17).
Postgres range partitioning requires the partition key (``created_at``) to be in the primary key,
hence the composite ``(id, created_at)`` PK. The ``PARTITION BY RANGE`` DDL and per-month child
tables are created in the Alembic migration, not expressible in the ORM model alone.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.enums import ActorType
from db.models.base import Base


class AuditLog(Base):
    """An append-only audit entry, correlated to an incident where applicable (CLAUDE.md §17)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Part of the PK because it is the partition key (monthly range partitioning).
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False
    )
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type"), nullable=False
    )
    actor_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str | None] = mapped_column(String, nullable=True)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    audit_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
