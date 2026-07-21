"""Long-term memory with a human approval gate (FR-7, ESD §9 step 7).

Drafts are generated on resolution (`approved_by IS NULL` = pending); nothing enters
retrievable memory until a human approves (optionally editing) it (FR-7.2). Retrieval is
always scoped by the compound ``(service_name, incident_type)`` key (FR-7.3) so unrelated
failure domains cannot cross-contaminate. Implemented on Postgres directly rather than
Mem0 — same interface, one fewer external dependency (documented in ESD §25).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Incident, MemorySummary, RemediationAction


async def draft_summary(
    session: AsyncSession,
    incident: Incident,
    *,
    root_cause_category: str,
    hypothesis: str,
) -> MemorySummary:
    """FR-7.1: structured summary (symptom, root cause, fix, outcome) — pending approval."""
    executed = (
        (
            await session.execute(
                select(RemediationAction).where(
                    RemediationAction.incident_id == incident.id,
                    RemediationAction.executed_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    fix = (
        "; ".join(
            f"{a.action_type} on {a.target_resource_id}" + (" (shadow)" if a.shadow else "")
            for a in executed
        )
        or "no automated action; resolved manually"
    )
    summary = MemorySummary(
        incident_id=incident.id,
        service_name=incident.service_name,
        incident_type=root_cause_category,
        symptom=incident.title,
        root_cause=hypothesis,
        fix=fix,
        outcome="resolved",
        approved_by=None,  # FR-7.2: a human must approve before this is retrievable
    )
    session.add(summary)
    await session.flush()
    return summary


async def approve_summary(
    session: AsyncSession,
    summary_id: uuid.UUID,
    *,
    approver_id: uuid.UUID,
    edits: dict[str, str] | None = None,
) -> MemorySummary | None:
    """FR-7.2: approve (with optional edits) — the write-back gate."""
    summary = await session.get(MemorySummary, summary_id)
    if summary is None:
        return None
    for field in ("symptom", "root_cause", "fix", "outcome"):
        if edits and field in edits:
            setattr(summary, field, edits[field])
    summary.approved_by = approver_id
    await session.flush()
    return summary


async def list_pending(session: AsyncSession) -> list[MemorySummary]:
    stmt = (
        select(MemorySummary)
        .where(MemorySummary.approved_by.is_(None))
        .order_by(MemorySummary.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def recall(
    session: AsyncSession,
    *,
    service_name: str,
    incident_type: str | None = None,
    limit: int = 3,
) -> list[MemorySummary]:
    """FR-7.3: approved memories only, scoped by the compound key."""
    stmt = (
        select(MemorySummary)
        .where(
            MemorySummary.service_name == service_name,
            MemorySummary.approved_by.is_not(None),
        )
        .order_by(MemorySummary.created_at.desc())
        .limit(limit)
    )
    if incident_type is not None:
        stmt = stmt.where(MemorySummary.incident_type == incident_type)
    return list((await session.execute(stmt)).scalars().all())


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)
