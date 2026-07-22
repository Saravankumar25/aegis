"""Integration tests: the RAG pipeline against a real Postgres + real embedding model.

Nothing is doubled here. Real chunking, real BGE vectors, real pgvector ANN search, real
Postgres full-text, real RRF, real cross-encoder reranking. The assertions target the
properties that would silently degrade answer quality if they broke — and one that already
did break in development, which now has a regression test.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.enums import RunbookSource
from db.models import Runbook, RunbookChunk
from rag.store import corpus_stats, search_runbooks, upsert_runbook

pytestmark = pytest.mark.embedding

OOM_DOC = """# OOM CrashLoop

## Symptoms

Pod restarts climb and the container's last state shows OOMKilled.

## Mitigation

Raise resources.limits.memory for the workload and redeploy it.

## Rollback

If raising the limit does not help, revert the change that increased usage.
"""

LATENCY_DOC = """# Latency degradation

## Symptoms

The p99 response time rises while the error rate stays flat.

## Mitigation

Check downstream database saturation and connection pool exhaustion.
"""


async def _seed(session: AsyncSession) -> None:
    await upsert_runbook(
        session,
        title="OOM CrashLoop",
        content=OOM_DOC,
        source=RunbookSource.internal,
        service_tags=["checkout-service"],
    )
    await upsert_runbook(
        session,
        title="Latency degradation",
        content=LATENCY_DOC,
        source=RunbookSource.internal,
        service_tags=["catalog-service"],
    )
    await session.commit()


async def test_ingestion_creates_multiple_chunks_per_document(session: AsyncSession):
    """A structured document must become several retrievable passages, not one blurred vector."""
    await _seed(session)
    stats = await corpus_stats(session)
    assert stats["documents"] == 2
    assert stats["chunks"] > stats["documents"]
    # Every chunk must be embedded, or retrieval silently ignores it.
    assert stats["embedded_chunks"] == stats["chunks"]


async def test_chunks_carry_their_section(session: AsyncSession):
    await _seed(session)
    paths = (await session.execute(select(RunbookChunk.heading_path))).scalars().all()
    assert any("Mitigation" in p for p in paths)
    assert any("Rollback" in p for p in paths)


async def test_semantic_retrieval_without_shared_vocabulary(session: AsyncSession):
    """The core justification for the embedder swap, end to end through pgvector."""
    await _seed(session)
    hits = await search_runbooks(session, "the container ran out of RAM and restarted", k=3)
    assert hits
    assert "OOM" in hits[0].title


async def test_lexical_retrieval_catches_exact_identifier(session: AsyncSession):
    """Embeddings are weakest on rare literal tokens; full-text is why hybrid exists."""
    await _seed(session)
    hits = await search_runbooks(session, "OOMKilled", k=3)
    assert hits
    assert "lexical" in hits[0].matched_by


async def test_metadata_filter_excludes_other_services(session: AsyncSession):
    """A runbook for a service that is not on fire is a distractor, not a result.

    The query is one the catalog runbook can actually answer. It used to be "restarts and
    memory", which only the *checkout* runbook covers — and the assertion passed because
    filtering to catalog-service returned the latency runbook regardless of whether it was
    relevant. The calibrated relevance floor now rejects that, correctly: matching a service
    tag is not the same as answering the question, and returning an unrelated runbook
    because it belongs to the right service is precisely the distractor this test names.
    """
    await _seed(session)
    hits = await search_runbooks(
        session, "response times climbing with no errors", k=5, service="catalog-service"
    )
    assert hits
    assert all("catalog-service" in h.service_tags for h in hits)


async def test_metadata_filter_does_not_rescue_an_irrelevant_runbook(session: AsyncSession):
    """Service scoping narrows candidates; it must not lower the relevance bar.

    Only the latency runbook is tagged `catalog-service`, so a memory question scoped to
    that service has no real answer. Returning the latency runbook anyway would hand RCA an
    authoritative-looking passage about the wrong failure mode.
    """
    await _seed(session)
    hits = await search_runbooks(session, "restarts and memory", k=5, service="catalog-service")
    assert hits == [], f"irrelevant runbook survived a service filter: {[h.title for h in hits]}"


async def test_citation_does_not_repeat_the_title(session: AsyncSession):
    """Regression: the H1 is usually the title, which rendered as "Title › Title"."""
    await _seed(session)
    hits = await search_runbooks(session, "raise the memory limit", k=3)
    assert hits
    for hit in hits:
        head, _, tail = hit.citation.partition(" › ")
        assert head != tail


async def test_reindex_is_a_noop_when_nothing_changed(session: AsyncSession):
    """Incremental indexing: unchanged content must not be re-embedded."""
    await _seed(session)
    before = await corpus_stats(session)
    _, changed = await upsert_runbook(
        session,
        title="OOM CrashLoop",
        content=OOM_DOC,
        source=RunbookSource.internal,
        service_tags=["checkout-service"],
    )
    await session.commit()
    assert changed is False
    assert await corpus_stats(session) == before


async def test_missing_chunks_force_reindex_even_when_content_matches(session: AsyncSession):
    """Regression for a real defect.

    Freshness was decided on the content hash alone, so after chunking was introduced every
    document reported "unchanged" and produced zero chunks — the corpus was silently
    unretrievable while ingestion logged success. Deleting the chunks reproduces exactly that
    state: identical content, empty index.
    """
    await _seed(session)
    runbook = (
        await session.execute(select(Runbook).where(Runbook.title == "OOM CrashLoop"))
    ).scalar_one()
    await session.execute(
        RunbookChunk.__table__.delete().where(RunbookChunk.runbook_id == runbook.id)
    )
    await session.commit()

    _, changed = await upsert_runbook(
        session,
        title="OOM CrashLoop",
        content=OOM_DOC,
        source=RunbookSource.internal,
        service_tags=["checkout-service"],
    )
    await session.commit()

    assert changed is True
    rebuilt = (
        await session.execute(
            select(func.count(RunbookChunk.id)).where(RunbookChunk.runbook_id == runbook.id)
        )
    ).scalar_one()
    assert rebuilt > 0


async def test_model_change_invalidates_the_index(session: AsyncSession):
    """A different embedding model produces a different vector space; reuse would be nonsense."""
    await _seed(session)
    runbook = (
        await session.execute(select(Runbook).where(Runbook.title == "OOM CrashLoop"))
    ).scalar_one()
    runbook.embedding_model = "some-other-model"
    await session.commit()

    _, changed = await upsert_runbook(
        session,
        title="OOM CrashLoop",
        content=OOM_DOC,
        source=RunbookSource.internal,
        service_tags=["checkout-service"],
    )
    await session.commit()
    assert changed is True


async def test_empty_corpus_returns_no_hits(session: AsyncSession):
    """Retrieval must degrade to "nothing found", never raise, on a cold corpus."""
    assert await search_runbooks(session, "anything at all", k=3) == []
