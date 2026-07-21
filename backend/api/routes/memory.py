"""V1.5 memory routes: pending queue + human approval gate (FR-7.2)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session, require_role
from api.errors import AegisError
from api.schemas import MemoryApproveIn, MemorySummaryOut
from db.enums import ActorType, UserRole
from db.models import User
from db.repository import AuditRepository
from memory.store import approve_summary, list_pending

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/pending", response_model=list[MemorySummaryOut])
async def pending(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.viewer, UserRole.on_call_engineer, UserRole.admin)),
) -> list[MemorySummaryOut]:
    """Draft summaries awaiting human approval before entering long-term memory."""
    return [MemorySummaryOut.model_validate(s) for s in await list_pending(session)]


@router.post("/{summary_id}/approve", response_model=MemorySummaryOut)
async def approve(
    summary_id: uuid.UUID,
    body: MemoryApproveIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_role(UserRole.on_call_engineer, UserRole.admin)),
) -> MemorySummaryOut:
    """Approve (optionally editing) a draft — the FR-7.2 write-back gate."""
    allowed = {"symptom", "root_cause", "fix", "outcome"}
    if body.edits and not set(body.edits) <= allowed:
        raise AegisError("validation_error", f"editable fields: {sorted(allowed)}", status_code=422)
    summary = await approve_summary(session, summary_id, approver_id=user.id, edits=body.edits)
    if summary is None:
        raise AegisError("not_found", "no such summary", status_code=404)
    await AuditRepository(session).write(
        actor_type=ActorType.human,
        actor_id=str(user.id),
        action="memory_summary_approved",
        incident_id=summary.incident_id,
        audit_metadata={"edited_fields": sorted(body.edits) if body.edits else []},
    )
    return MemorySummaryOut.model_validate(summary)
