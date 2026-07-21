"""Deterministic retrieval metrics (ESD §22).

Implemented natively rather than pulled from a framework because these are ~40 lines of
arithmetic over ranked lists, and the value of running them **on every commit with no model,
no network and no quota** far outweighs the dependency. RAGAS's retrieval metrics are
LLM-judged, which makes them unusable as a per-commit gate on a free tier.

The metric set is chosen for what each one catches:

* **hit rate** — did the right document appear at all? Catches total retrieval failure.
* **MRR** — how high did it rank? Catches quality decay that hit rate hides, because a
  correct answer at rank 5 still "hits" while being useless in a top-3 prompt.
* **precision@k** — how much of what we sent the model was relevant? Directly predicts how
  much distractor text lands in the RCA prompt.
* **NDCG@k** — rank-weighted, so a correct answer moving from rank 1 to rank 3 registers as
  a regression even when every other metric stays flat.
* **forbidden rate** — did a known distractor appear? The metric the others cannot express.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Per-case scores, kept so a failing suite names the case rather than a mean."""

    case_id: str
    hit: bool
    reciprocal_rank: float
    precision_at_k: float
    ndcg_at_k: float
    forbidden_hit: bool
    retrieved_titles: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.hit and not self.forbidden_hit


@dataclass(slots=True)
class RetrievalReport:
    """Aggregate report over a golden set."""

    results: list[CaseResult] = field(default_factory=list)

    def _mean(self, attr: str) -> float:
        if not self.results:
            return 0.0
        return sum(getattr(r, attr) for r in self.results) / len(self.results)

    @property
    def hit_rate(self) -> float:
        return self._mean("hit")

    @property
    def mrr(self) -> float:
        return self._mean("reciprocal_rank")

    @property
    def precision_at_k(self) -> float:
        return self._mean("precision_at_k")

    @property
    def ndcg_at_k(self) -> float:
        return self._mean("ndcg_at_k")

    @property
    def forbidden_rate(self) -> float:
        """Fraction of cases that surfaced a known distractor. Lower is better."""
        return self._mean("forbidden_hit")

    @property
    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]

    def summary(self) -> str:
        return (
            f"cases={len(self.results)} hit_rate={self.hit_rate:.2f} mrr={self.mrr:.2f} "
            f"p@k={self.precision_at_k:.2f} ndcg@k={self.ndcg_at_k:.2f} "
            f"forbidden={self.forbidden_rate:.2f}"
        )


def _matches(title: str, needles: tuple[str, ...]) -> bool:
    lowered = title.lower()
    return any(n.lower() in lowered for n in needles)


def score_case(
    *,
    case_id: str,
    retrieved_titles: list[str],
    expected_titles: tuple[str, ...],
    forbidden_titles: tuple[str, ...] = (),
) -> CaseResult:
    """Score one case against its ranked retrieval result."""
    forbidden_hit = (
        any(_matches(t, forbidden_titles) for t in retrieved_titles) if forbidden_titles else False
    )

    if not expected_titles:
        # An unanswerable case: the correct behaviour is returning nothing relevant. Scoring
        # it like a normal case would reward retrieving a confident irrelevant chunk.
        empty = len(retrieved_titles) == 0
        return CaseResult(
            case_id=case_id,
            hit=empty,
            reciprocal_rank=1.0 if empty else 0.0,
            precision_at_k=1.0 if empty else 0.0,
            ndcg_at_k=1.0 if empty else 0.0,
            forbidden_hit=forbidden_hit,
            retrieved_titles=tuple(retrieved_titles),
        )

    relevance = [1 if _matches(t, expected_titles) else 0 for t in retrieved_titles]
    hit = any(relevance)
    rank = relevance.index(1) + 1 if hit else 0
    reciprocal_rank = 1.0 / rank if rank else 0.0
    precision = sum(relevance) / len(relevance) if relevance else 0.0

    dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(relevance))
    # Ideal ranking puts every relevant document first.
    ideal = sum(1 / math.log2(i + 2) for i in range(min(sum(relevance), len(relevance))))
    ndcg = dcg / ideal if ideal else 0.0

    return CaseResult(
        case_id=case_id,
        hit=hit,
        reciprocal_rank=reciprocal_rank,
        precision_at_k=precision,
        ndcg_at_k=ndcg,
        forbidden_hit=forbidden_hit,
        retrieved_titles=tuple(retrieved_titles),
    )


async def evaluate_retrieval(session, cases, *, k: int = 5) -> RetrievalReport:
    """Run the golden set against the live retrieval pipeline.

    Deliberately calls `search_runbooks` rather than a reimplementation, so the metric
    measures what production actually does — including chunking, RRF fusion, metadata
    filtering and reranking.
    """
    from rag.store import search_runbooks

    report = RetrievalReport()
    for case in cases:
        hits = await search_runbooks(session, case.query, k=k, service=case.service)
        report.results.append(
            score_case(
                case_id=case.id,
                retrieved_titles=[h.title for h in hits],
                expected_titles=case.expected_titles,
                forbidden_titles=case.forbidden_titles,
            )
        )
    return report
