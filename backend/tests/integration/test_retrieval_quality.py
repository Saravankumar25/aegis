"""RAG quality regression gate (ESD §22).

Runs the golden retrieval set against the **real** pipeline — real BGE embeddings, real
pgvector ANN, real Postgres full-text, real RRF, real cross-encoder reranking — and fails the
build when quality drops below the committed thresholds.

This is the test that makes "the RAG pipeline is good" a measurement instead of a claim. It
needs no judge model and no API quota, so it can gate every commit; the LLM-judged metrics
(`evaluation.ragas_metrics`) are a separate, on-demand check.

Thresholds are set at the level the pipeline currently clears, not aspirationally. A gate
tuned above current performance fails permanently and gets disabled, which is worse than no
gate; one tuned far below never fires. Raise them when the pipeline genuinely improves.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from db.enums import RunbookSource
from evaluation import GOLDEN_RETRIEVAL_CASES, evaluate_retrieval
from evaluation.retrieval_metrics import score_case
from rag.store import upsert_runbook

pytestmark = pytest.mark.embedding

# Committed quality floor. Changing any of these downward is a deliberate act that shows up
# in review as exactly what it is: accepting worse retrieval.
MIN_HIT_RATE = 0.80
MIN_MRR = 0.70
MIN_NDCG = 0.70
MAX_FORBIDDEN_RATE = 0.0  # a known distractor surfacing is never acceptable

_CORPUS = {
    "Runbook: OOMKilled / CrashLoopBackOff pods": (
        "# Runbook: OOMKilled / CrashLoopBackOff pods\n\n"
        "## Symptoms\n\nPod restarts climbing, container state waiting with reason "
        "CrashLoopBackOff, lastState terminated with reason OOMKilled.\n\n"
        "## Mitigation\n\nRaise the memory limit deliberately, or revert the change that "
        "increased usage.\n"
    ),
    "Runbook: p99 latency degradation": (
        "# Runbook: p99 latency degradation\n\n"
        "## Symptoms\n\nResponse times rise while the error rate stays flat; requests take "
        "seconds rather than milliseconds.\n\n"
        "## Mitigation\n\nCheck downstream saturation and connection pool exhaustion.\n"
    ),
    "Runbook: elevated 5xx error rate right after a deploy": (
        "# Runbook: elevated 5xx error rate right after a deploy\n\n"
        "## Symptoms\n\nErrors climb immediately following a release or rollout.\n\n"
        "## Mitigation\n\nRoll back the offending release.\n"
    ),
    "Runbook: service partially or fully unavailable": (
        "# Runbook: service partially or fully unavailable\n\n"
        "## Symptoms\n\nRequests fail entirely; the service does not respond and customers "
        "cannot reach it at all.\n\n"
        "## Mitigation\n\nCheck readiness probes and replica availability.\n"
    ),
}

_TAGS = ["checkout-service", "payment-service", "catalog-service"]


async def _seed_corpus(session: AsyncSession) -> None:
    for title, content in _CORPUS.items():
        await upsert_runbook(
            session,
            title=title,
            content=content,
            source=RunbookSource.internal,
            service_tags=_TAGS,
        )
    await session.commit()


@pytest.fixture
async def corpus(session: AsyncSession):
    await _seed_corpus(session)
    return session


async def test_retrieval_meets_quality_thresholds(corpus):
    """The gate. A prompt, model, chunking or fusion change that degrades RAG fails here."""
    report = await evaluate_retrieval(corpus, GOLDEN_RETRIEVAL_CASES, k=5)

    detail = "\n".join(
        f"  {r.case_id}: hit={r.hit} rr={r.reciprocal_rank:.2f} "
        f"forbidden={r.forbidden_hit} got={list(r.retrieved_titles)[:3]}"
        for r in report.results
    )
    assert report.hit_rate >= MIN_HIT_RATE, f"hit rate {report.hit_rate:.2f}\n{detail}"
    assert report.mrr >= MIN_MRR, f"MRR {report.mrr:.2f}\n{detail}"
    assert report.ndcg_at_k >= MIN_NDCG, f"NDCG {report.ndcg_at_k:.2f}\n{detail}"
    assert report.forbidden_rate <= MAX_FORBIDDEN_RATE, f"distractors surfaced\n{detail}"


async def test_semantic_cases_pass_without_lexical_overlap(corpus):
    """The specific capability the embedder swap bought — asserted, not assumed."""
    semantic = [c for c in GOLDEN_RETRIEVAL_CASES if "semantic" in c.tags]
    report = await evaluate_retrieval(corpus, semantic, k=5)
    failures = [r.case_id for r in report.failures]
    assert not failures, f"semantic retrieval failed for: {failures}"


async def test_metadata_filter_returns_only_the_named_service(corpus):
    from rag.store import search_runbooks

    hits = await search_runbooks(corpus, "pods restarting", k=5, service="catalog-service")
    assert hits
    assert all("catalog-service" in h.service_tags for h in hits)


# --- metric correctness (the gate is only as trustworthy as its arithmetic) ----------------


def test_perfect_ranking_scores_one():
    result = score_case(
        case_id="t", retrieved_titles=["OOMKilled runbook"], expected_titles=("OOMKilled",)
    )
    assert result.hit and result.reciprocal_rank == 1.0 and result.ndcg_at_k == 1.0


def test_reciprocal_rank_degrades_with_position():
    first = score_case(case_id="a", retrieved_titles=["OOM", "x"], expected_titles=("OOM",))
    third = score_case(case_id="b", retrieved_titles=["x", "y", "OOM"], expected_titles=("OOM",))
    assert first.reciprocal_rank > third.reciprocal_rank


def test_forbidden_title_is_flagged_even_when_the_expected_one_ranks():
    result = score_case(
        case_id="t",
        retrieved_titles=["latency runbook", "OOMKilled runbook"],
        expected_titles=("latency",),
        forbidden_titles=("OOMKilled",),
    )
    assert result.hit is True
    assert result.forbidden_hit is True
    assert result.passed is False  # a hit does not excuse a distractor


def test_unanswerable_case_rewards_returning_nothing():
    empty = score_case(case_id="t", retrieved_titles=[], expected_titles=())
    confident = score_case(case_id="t", retrieved_titles=["OOMKilled"], expected_titles=())
    assert empty.passed is True
    assert confident.passed is False
