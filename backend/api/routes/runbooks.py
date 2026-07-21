"""Runbook RAG search endpoint (ESD §7: GET /runbooks/search)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_session
from api.schemas import RunbookHit
from db.models import User
from rag.store import search_runbooks

router = APIRouter(prefix="/runbooks", tags=["runbooks"])


@router.get("/search", response_model=list[RunbookHit])
async def search(
    q: str = Query(min_length=2, max_length=500),
    k: int = Query(default=3, ge=1, le=10),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[RunbookHit]:
    """Cosine search over the runbook/postmortem corpus (FR-3.3)."""
    hits = await search_runbooks(session, q, k=k)
    return [
        RunbookHit(
            id=r.id,
            title=r.title,
            snippet=r.content[:400],
            score=score,
            source=r.source,
            service_tags=list(r.service_tags or []),
        )
        for r, score in hits
    ]
