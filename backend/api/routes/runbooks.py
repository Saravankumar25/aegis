"""Runbook RAG search endpoint (ESD §7: GET /runbooks/search)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_current_user, get_session
from api.schemas import RunbookHit
from core.config import get_settings
from db.models import User
from rag.store import search_runbooks

router = APIRouter(prefix="/runbooks", tags=["runbooks"])


@router.get("/search", response_model=list[RunbookHit])
async def search(
    q: str = Query(min_length=2, max_length=500),
    k: int = Query(default=0, ge=0, le=20, description="0 uses the configured default."),
    service: str | None = Query(
        default=None,
        max_length=128,
        description="Restrict to chunks tagged for this service (metadata filter).",
    ),
    rerank: bool | None = Query(
        default=None, description="Override cross-encoder reranking for this query."
    ),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(get_current_user),
) -> list[RunbookHit]:
    """Hybrid (semantic + lexical) chunk retrieval over the runbook corpus (FR-3.3)."""
    hits = await search_runbooks(
        session,
        q,
        k=k or get_settings().rag_top_k,
        service=service,
        rerank=rerank,
    )
    return [
        RunbookHit(
            id=uuid.UUID(hit.chunk_id),
            runbook_id=uuid.UUID(hit.runbook_id),
            title=hit.title,
            heading_path=hit.heading_path,
            citation=hit.citation,
            snippet=hit.content[:400],
            score=hit.score,
            source=hit.source,
            service_tags=hit.service_tags,
            matched_by=hit.matched_by,
        )
        for hit in hits
    ]
