"""Runbook store: content-hash-versioned ingestion + pgvector cosine search (FR-3.3).

``content_hash`` makes stale citations detectable (ESD §6): if a runbook's content changes,
its hash changes, and an old citation no longer matches the stored row's hash.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.enums import RunbookSource
from db.models import Runbook
from rag.embedding import get_embedder
from redaction.pipeline import redact


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


async def upsert_runbook(
    session: AsyncSession,
    *,
    title: str,
    content: str,
    source: RunbookSource,
    service_tags: list[str],
    source_company: str | None = None,
) -> tuple[Runbook, bool]:
    """Insert or refresh a runbook; unchanged content (same hash) is a no-op.

    Content is redacted before embedding/storage — corpus documents are an untrusted
    evidence source like any other (CLAUDE.md §17).
    """
    clean = redact(content).text
    digest = content_hash(clean)
    existing = (
        await session.execute(select(Runbook).where(Runbook.title == title))
    ).scalar_one_or_none()
    if existing is not None and existing.content_hash == digest:
        return existing, False
    embedding = get_embedder().embed(f"{title}\n{clean}")
    if existing is not None:
        existing.content = clean
        existing.content_hash = digest
        existing.embedding = embedding
        existing.service_tags = service_tags
        await session.flush()
        return existing, True
    runbook = Runbook(
        title=title,
        content=clean,
        content_hash=digest,
        source=source,
        source_company=source_company,
        service_tags=service_tags,
        embedding=embedding,
    )
    session.add(runbook)
    await session.flush()
    return runbook, True


async def search_runbooks(
    session: AsyncSession, query: str, *, k: int = 3
) -> list[tuple[Runbook, float]]:
    """Top-k cosine matches for a query. Score = 1 - cosine distance (higher is better)."""
    query_vec = get_embedder().embed(query)
    distance = Runbook.embedding.cosine_distance(query_vec)
    rows = (
        await session.execute(
            select(Runbook, distance.label("distance")).order_by(distance).limit(k)
        )
    ).all()
    return [(row.Runbook, round(1.0 - row.distance, 4)) for row in rows]
