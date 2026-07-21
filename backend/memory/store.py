"""Long-term memory with a human approval gate (FR-7, ESD §9 step 7).

Drafts are generated on resolution (`approved_by IS NULL` = pending); nothing enters
retrievable memory until a human approves (optionally editing) it (FR-7.2). Retrieval is
always scoped by the compound ``(service_name, incident_type)`` key (FR-7.3) so unrelated
failure domains cannot cross-contaminate. Implemented on Postgres directly rather than
Mem0 — same interface, one fewer external dependency (documented in ESD §25).
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Incident, MemorySummary, RemediationAction
from memory.agent import RecallOutcome, select_relevant, write_lesson
from providers.base import LLMProvider


async def draft_summary(
    session: AsyncSession,
    incident: Incident,
    *,
    root_cause_category: str,
    hypothesis: str,
    provider: LLMProvider | None = None,
) -> MemorySummary:
    """FR-7.1: structured summary (symptom, root cause, fix, outcome) — pending approval.

    The Memory agent writes the lesson (``memory.agent.write_lesson``). Without a model the
    raw facts are recorded instead and the draft says so, because a summary that *reads* as
    written but was assembled by string concatenation is the harder failure to notice: a
    human approving it cannot tell that `symptom` is the alert's wording rather than what an
    engineer would actually search by.
    """
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
    actions = (
        "; ".join(
            f"{a.action_type} on {a.target_resource_id}" + (" (shadow)" if a.shadow else "")
            for a in executed
        )
        or "no automated action; resolved manually"
    )

    lesson = await write_lesson(
        provider,
        title=incident.title,
        service=incident.service_name,
        severity=str(incident.severity),
        root_cause_category=root_cause_category,
        hypothesis=hypothesis,
        actions=actions,
        outcome="resolved",
    )

    if lesson is not None:
        symptom, root_cause, fix, outcome = (
            lesson.symptom,
            lesson.root_cause,
            lesson.fix,
            lesson.outcome,
        )
    else:
        symptom, root_cause, fix = incident.title, hypothesis, actions
        outcome = "resolved (not distilled — no model available at resolution time)"

    summary = MemorySummary(
        incident_id=incident.id,
        service_name=incident.service_name,
        incident_type=root_cause_category,
        symptom=symptom,
        root_cause=root_cause,
        fix=fix,
        outcome=outcome,
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
    """FR-7.3: approved memories only, scoped by the compound key.

    The deterministic half of recall, and the half that carries the safety property: only
    ``approved_by IS NOT NULL`` rows are ever returned. `recall_relevant` layers LLM
    relevance judgement on top and can only narrow this set.
    """
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


async def recall_relevant(
    session: AsyncSession,
    provider: LLMProvider | None,
    *,
    service_name: str,
    title: str,
    kind: str,
    symptoms: str,
    incident_type: str | None = None,
    candidate_limit: int = 10,
) -> RecallOutcome:
    """Recall memories that actually inform this incident.

    Fetches a wider candidate pool than the old top-3-by-recency and lets the Memory agent
    judge which apply. Widening the *candidate* pool is safe because every candidate is
    already human-approved for this service; what changes is that relevance, not recency,
    decides which reach the investigation.
    """
    candidates = await recall(
        session,
        service_name=service_name,
        incident_type=incident_type,
        limit=candidate_limit,
    )
    return await select_relevant(
        provider,
        candidates,
        title=title,
        service=service_name,
        kind=kind,
        symptoms=symptoms,
    )
