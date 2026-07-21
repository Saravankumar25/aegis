"""Runbook ingestion and hybrid retrieval (FR-3.3, ESD §20).

Ingestion: document → redact → chunk → embed → upsert chunks. ``content_hash`` versions the
document, so re-ingesting unchanged content is a no-op and a changed document re-chunks
atomically. This is the incremental-indexing path: only what actually changed is re-embedded.

Retrieval fuses two independent rankings with **Reciprocal Rank Fusion**:

* semantic (pgvector cosine over chunk embeddings), which matches meaning — "out of memory"
  finding a passage that only ever says ``OOMKilled``;
* lexical (Postgres full-text over the same chunks), which matches the exact identifiers
  embeddings are weakest on — error codes, flag names, service names.

RRF is used rather than a weighted score blend because the two retrievers' scores are not
commensurable: a cosine distance and a ``ts_rank`` occupy different scales with different
distributions, so any fixed weighting is arbitrary and drifts as the corpus changes. RRF
consumes only *ranks*, which are comparable by construction.

The fused shortlist is then reordered by a cross-encoder (``rag.reranker``) when one is
available. Every stage degrades independently: no reranker → fused order; no lexical hits →
semantic order; no embedder → lexical only. Retrieval returns the best ranking it can rather
than failing an incident query.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.logging import get_logger
from db.enums import RunbookSource
from db.models import Runbook, RunbookChunk
from rag.chunking import ChunkConfig, chunk_markdown
from rag.embedding import Embedder, get_embedder
from rag.reranker import get_reranker
from redaction.pipeline import redact

_log = get_logger(component="rag")


def content_hash(text_value: str) -> str:
    return hashlib.sha256(text_value.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """One retrieval result, carrying enough provenance to cite it precisely."""

    chunk_id: str
    runbook_id: str
    title: str
    heading_path: str
    content: str
    score: float
    source: str
    service_tags: list[str]
    # Which retrievers found it — surfaced so a low-quality result can be diagnosed rather
    # than merely observed.
    matched_by: list[str]

    @property
    def citation(self) -> str:
        """Human-readable provenance: document plus the section within it.

        The document's H1 is usually also its title, so the raw heading path would render as
        "Runbook: OOM › Runbook: OOM". The leading segment is dropped when it merely repeats
        the title — a citation that stutters reads as a bug to whoever is trusting it.
        """
        if not self.heading_path:
            return self.title
        segments = [s for s in self.heading_path.split(" › ") if s.strip()]
        if segments and segments[0].strip() == self.title.strip():
            segments = segments[1:]
        return f"{self.title} › {' › '.join(segments)}" if segments else self.title


def _chunk_config() -> ChunkConfig:
    settings = get_settings()
    return ChunkConfig(
        max_chars=settings.chunk_max_chars,
        overlap_chars=settings.chunk_overlap_chars,
        min_chars=settings.chunk_min_chars,
    )


async def _index_is_current(
    session: AsyncSession, runbook: Runbook, digest: str, embedder: Embedder
) -> bool:
    """Is this document's chunk index actually usable as-is?

    Content equality alone is **not** sufficient, and treating it as sufficient was a real
    defect: after chunking was introduced, ingestion reported every document "unchanged" and
    built zero chunks, leaving the corpus silently unretrievable. An index is current only if
    all four of these hold — content, model, dimension, and the chunks genuinely existing.
    """
    if runbook.content_hash != digest:
        return False
    if runbook.embedding_model != embedder.name or runbook.embedding_dim != embedder.dim:
        # A different model produces vectors in a different space; comparing them to query
        # vectors from the current model yields numbers that look valid and mean nothing.
        return False
    chunk_count = (
        await session.execute(
            select(func.count(RunbookChunk.id)).where(RunbookChunk.runbook_id == runbook.id)
        )
    ).scalar_one()
    return chunk_count > 0


async def upsert_runbook(
    session: AsyncSession,
    *,
    title: str,
    content: str,
    source: RunbookSource,
    service_tags: list[str],
    source_company: str | None = None,
) -> tuple[Runbook, bool]:
    """Insert or refresh a runbook and its chunk index.

    Content is redacted before chunking, embedding, or storage — corpus documents are an
    untrusted evidence source like any other (CLAUDE.md §17), and redacting after embedding
    would leave the sensitive text encoded in the vector.
    """
    clean = redact(content).text
    digest = content_hash(clean)
    embedder = get_embedder()

    existing = (
        await session.execute(select(Runbook).where(Runbook.title == title))
    ).scalar_one_or_none()

    if existing is not None and await _index_is_current(session, existing, digest, embedder):
        # Genuinely unchanged: skip re-embedding. This is what makes re-running ingestion over
        # a large corpus cheap, and why ingestion can be scheduled rather than manual.
        return existing, False

    if existing is not None:
        runbook = existing
        runbook.content = clean
        runbook.content_hash = digest
        runbook.service_tags = service_tags
        # Chunk boundaries move when content changes, so stale chunks cannot be updated in
        # place — they are replaced wholesale within this transaction.
        await session.execute(delete(RunbookChunk).where(RunbookChunk.runbook_id == runbook.id))
    else:
        runbook = Runbook(
            title=title,
            content=clean,
            content_hash=digest,
            source=source,
            source_company=source_company,
            service_tags=service_tags,
        )
        session.add(runbook)
    # Stamped from the embedder that is about to run, so provenance can never disagree with
    # the vectors actually stored.
    runbook.embedding_model = embedder.name
    runbook.embedding_dim = embedder.dim
    await session.flush()

    chunks = chunk_markdown(clean, _chunk_config())
    if not chunks:
        _log.warning("runbook_produced_no_chunks", title=title)
        return runbook, True

    vectors = await embedder.embed_passages([c.embedding_text for c in chunks])
    for chunk, vector in zip(chunks, vectors, strict=True):
        session.add(
            RunbookChunk(
                runbook_id=runbook.id,
                chunk_index=chunk.index,
                content=chunk.content,
                heading_path=chunk.heading_path,
                service_tags=service_tags,
                embedding=vector,
            )
        )
    await session.flush()
    _log.info("runbook_indexed", title=title, chunks=len(chunks))
    return runbook, True


def _rrf(rankings: list[list[str]], k: int) -> dict[str, float]:
    """Reciprocal Rank Fusion over per-retriever ordered id lists."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            fused[item_id] = fused.get(item_id, 0.0) + 1.0 / (k + rank)
    return fused


async def _semantic_candidates(
    session: AsyncSession, query: str, *, limit: int, service: str | None
) -> list[RunbookChunk]:
    try:
        query_vec = await get_embedder().embed_query(query)
    except Exception as exc:  # noqa: BLE001 — fall back to lexical rather than fail the search
        _log.warning("semantic_retrieval_unavailable", error=str(exc))
        return []
    distance = RunbookChunk.embedding.cosine_distance(query_vec)
    stmt = select(RunbookChunk).where(RunbookChunk.embedding.isnot(None))
    if service:
        stmt = stmt.where(RunbookChunk.service_tags.any(service))
    rows = (await session.execute(stmt.order_by(distance).limit(limit))).scalars().all()
    return list(rows)


async def _lexical_candidates(
    session: AsyncSession, query: str, *, limit: int, service: str | None
) -> list[RunbookChunk]:
    # plainto_tsquery (not to_tsquery) because the input is free text, not tsquery syntax;
    # passing raw user text to to_tsquery raises on ordinary punctuation.
    tsquery = func.plainto_tsquery("english", query)
    stmt = select(RunbookChunk).where(RunbookChunk.search_vector.op("@@")(tsquery))
    if service:
        stmt = stmt.where(RunbookChunk.service_tags.any(service))
    stmt = stmt.order_by(func.ts_rank(RunbookChunk.search_vector, tsquery).desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def search_runbooks(
    session: AsyncSession,
    query: str,
    *,
    k: int | None = None,
    service: str | None = None,
    rerank: bool | None = None,
) -> list[RetrievedChunk]:
    """Hybrid retrieval over runbook chunks.

    ``service`` applies metadata filtering *inside* both retrievers rather than filtering
    their output, so the candidate pool is spent on rows that can actually qualify.
    """
    settings = get_settings()
    k = k or settings.rag_top_k
    rerank = settings.rag_rerank_enabled if rerank is None else rerank
    candidate_k = max(settings.rag_candidate_k, k)

    semantic = await _semantic_candidates(session, query, limit=candidate_k, service=service)
    lexical = await _lexical_candidates(session, query, limit=candidate_k, service=service)
    if not semantic and not lexical:
        return []

    by_id: dict[str, RunbookChunk] = {}
    matched: dict[str, list[str]] = {}
    for retriever, rows in (("semantic", semantic), ("lexical", lexical)):
        for row in rows:
            by_id[str(row.id)] = row
            matched.setdefault(str(row.id), []).append(retriever)

    fused = _rrf([[str(r.id) for r in semantic], [str(r.id) for r in lexical]], settings.rag_rrf_k)
    ordered_ids = sorted(fused, key=lambda i: fused[i], reverse=True)

    # Load parent titles in one query rather than lazy-loading per chunk (an N+1 that would be
    # invisible on a 4-document corpus and painful on a real one).
    runbook_ids = {by_id[i].runbook_id for i in ordered_ids}
    runbooks = {
        r.id: r
        for r in (
            (await session.execute(select(Runbook).where(Runbook.id.in_(runbook_ids))))
            .scalars()
            .all()
        )
    }

    shortlist = ordered_ids[:candidate_k]
    scores: dict[str, float] = {i: fused[i] for i in shortlist}

    if rerank and len(shortlist) > 1:
        passages = [by_id[i].content for i in shortlist]
        reranked = await get_reranker().score(query, passages)
        if reranked is not None:
            scores = dict(zip(shortlist, reranked, strict=True))
            shortlist = sorted(shortlist, key=lambda i: scores[i], reverse=True)

    results: list[RetrievedChunk] = []
    for chunk_id in shortlist[:k]:
        chunk = by_id[chunk_id]
        parent = runbooks.get(chunk.runbook_id)
        score = float(scores[chunk_id])
        if settings.rag_min_score and score < settings.rag_min_score:
            continue
        results.append(
            RetrievedChunk(
                chunk_id=chunk_id,
                runbook_id=str(chunk.runbook_id),
                title=parent.title if parent else "(unknown runbook)",
                heading_path=chunk.heading_path,
                content=chunk.content,
                score=round(score, 4),
                source=str(parent.source) if parent else "internal",
                service_tags=list(chunk.service_tags or []),
                matched_by=matched.get(chunk_id, []),
            )
        )
    return results


async def corpus_stats(session: AsyncSession) -> dict[str, int]:
    """Index size, for the metrics endpoint and ingestion verification."""
    documents = (await session.execute(select(func.count(Runbook.id)))).scalar_one()
    chunks = (await session.execute(select(func.count(RunbookChunk.id)))).scalar_one()
    embedded = (
        await session.execute(
            select(func.count(RunbookChunk.id)).where(RunbookChunk.embedding.isnot(None))
        )
    ).scalar_one()
    return {"documents": documents, "chunks": chunks, "embedded_chunks": embedded}
