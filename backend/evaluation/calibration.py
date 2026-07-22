"""Empirical calibration of the retrieval relevance floor (ESD §20, PRD FR-3.3).

`rag_min_score` shipped as `0.0` — the filter disabled — and the reason it stayed disabled
is that there was nothing to set it *to*. The score it filters on is whichever the pipeline
last produced: a Reciprocal Rank Fusion score (~0.016–0.03, a rank artefact with no
relevance meaning) when reranking is off, and an unbounded cross-encoder logit when it is on.
A single number cannot threshold both scales, so any hand-picked value was guaranteed to be
either inert or arbitrary.

This module fixes the premise rather than the number. It measures the cross-encoder's actual
score distribution over the golden dataset, sweeps candidate thresholds, and reports what
each one costs and buys. The floor applies **only** to reranked scores, because only those
carry relevance meaning; that constraint is enforced in `rag.store`, not merely documented.

Measured on the shipped corpus with `Xenova/ms-marco-MiniLM-L-6-v2`:

    truly out-of-domain ("what is the capital of France")   logit ~ -11.2
    relevant, no shared vocabulary                          logit ~  -4.2
    relevant, shared vocabulary                             logit ~  +2.6

The gap between out-of-domain and weakly-relevant is roughly 7 logits, which is what makes a
threshold viable at all. It is also why the chosen value sits well below zero: a naive
"score > 0" floor would discard the paraphrased-query case that the semantic embedder exists
to serve, which is precisely the retrieval this system needs most.

Run:  python -m evaluation.calibration
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from evaluation.dataset import GOLDEN_RETRIEVAL_CASES, RetrievalCase
from evaluation.retrieval_metrics import _matches
from rag.store import search_runbooks

# Swept in logit space, which is where the model actually scores. Sigmoid compresses the
# entire interesting region (-11 to -4) into 0.00001–0.014, where floating-point comparison
# is uncomfortable and a human reading the config cannot tell the values apart.
CANDIDATE_THRESHOLDS: tuple[float, ...] = (
    -12.0,
    -11.0,
    -10.5,
    -10.0,
    -9.5,
    -9.0,
    -8.5,
    -8.0,
    -7.0,
    -6.0,
    -5.0,
    -4.0,
    -2.0,
    0.0,
)


@dataclass(frozen=True, slots=True)
class ThresholdOutcome:
    """What one candidate threshold does to the golden set."""

    threshold: float
    # Cases whose expected document still ranks after filtering.
    hits: int
    answerable_cases: int
    # Cases that should return nothing and do.
    correct_refusals: int
    unanswerable_cases: int
    # Forbidden (distractor) documents that survive the filter.
    forbidden_survivors: int
    # Relevant chunks lost to the filter — the cost side of the trade.
    relevant_dropped: int

    @property
    def hit_rate(self) -> float:
        return self.hits / self.answerable_cases if self.answerable_cases else 0.0

    @property
    def refusal_rate(self) -> float:
        if not self.unanswerable_cases:
            return 1.0
        return self.correct_refusals / self.unanswerable_cases

    @property
    def viable(self) -> bool:
        """Every answerable case still finds its document, and every unanswerable one returns
        nothing.

        Both conditions, not a weighted score: a floor that silently drops the runbook an
        operator needed is a worse failure than one that lets a distractor through, and
        averaging the two would let a good mean hide either.

        `relevant_dropped` is deliberately *not* a veto. It counts individual relevant chunks
        removed, and once a document is chunked into sections a case legitimately has several
        — dropping a weaker section while still retrieving the right document is precision
        improving, not an answer lost. Treating it as a veto rejected every threshold on the
        chunked corpus even at hit_rate 1.00. It stays in the report as the cost side of the
        trade, which is what it actually measures.
        """
        return self.hit_rate == 1.0 and self.refusal_rate == 1.0


def sigmoid(logit: float) -> float:
    """Relevance as a 0..1 probability, for display and for the API surface."""
    return 1.0 / (1.0 + math.exp(-logit))


async def _score_case(session: AsyncSession, case: RetrievalCase) -> list[tuple[float, bool]]:
    """Return ``(score, is_relevant)`` for every chunk retrieved for one case.

    Retrieval runs with the floor disabled so calibration observes the full distribution;
    thresholds are then applied here rather than inside the query.
    """
    hits = await search_runbooks(
        session, case.query, service=case.service, min_score_override=-math.inf
    )
    scored: list[tuple[float, bool]] = []
    for hit in hits:
        relevant = bool(case.expected_titles) and _matches(hit.title, case.expected_titles)
        scored.append((hit.score, relevant))
    return scored


async def sweep(session: AsyncSession) -> list[ThresholdOutcome]:
    """Measure every candidate threshold against the golden dataset."""
    per_case = [(case, await _score_case(session, case)) for case in GOLDEN_RETRIEVAL_CASES]

    outcomes: list[ThresholdOutcome] = []
    for threshold in CANDIDATE_THRESHOLDS:
        hits = answerable = correct_refusals = unanswerable = 0
        forbidden_survivors = relevant_dropped = 0

        for case, scored in per_case:
            surviving = [(s, rel) for s, rel in scored if s >= threshold]
            if case.expected_titles:
                answerable += 1
                if any(rel for _, rel in surviving):
                    hits += 1
                # Relevant chunks the filter removed — the cost side of the trade.
                relevant_dropped += sum(1 for s, rel in scored if rel and s < threshold)
            else:
                unanswerable += 1
                if not surviving:
                    correct_refusals += 1
                forbidden_survivors += len(surviving)

        outcomes.append(
            ThresholdOutcome(
                threshold=threshold,
                hits=hits,
                answerable_cases=answerable,
                correct_refusals=correct_refusals,
                unanswerable_cases=unanswerable,
                forbidden_survivors=forbidden_survivors,
                relevant_dropped=relevant_dropped,
            )
        )
    return outcomes


def recommend(outcomes: list[ThresholdOutcome]) -> ThresholdOutcome | None:
    """The most permissive viable threshold.

    Most permissive rather than most aggressive: among thresholds that refuse every
    out-of-domain query without losing a relevant document, the lowest leaves the widest
    margin before a slightly-harder future query falls under the floor. Picking the tightest
    viable value would fit the threshold to this corpus rather than to the score distribution.
    """
    viable = [o for o in outcomes if o.viable]
    return min(viable, key=lambda o: o.threshold) if viable else None


def format_report(outcomes: list[ThresholdOutcome]) -> str:
    lines = [
        "Retrieval relevance floor — threshold sweep",
        "",
        f"{'threshold':>10} {'sigmoid':>9} {'hit_rate':>9} {'refusal':>8} "
        f"{'distractors':>12} {'rel_dropped':>12} {'viable':>7}",
    ]
    for o in outcomes:
        lines.append(
            f"{o.threshold:>10.1f} {sigmoid(o.threshold):>9.5f} {o.hit_rate:>9.2f} "
            f"{o.refusal_rate:>8.2f} {o.forbidden_survivors:>12d} "
            f"{o.relevant_dropped:>12d} {'yes' if o.viable else '':>7}"
        )
    best = recommend(outcomes)
    lines.append("")
    if best is None:
        lines.append(
            "NO VIABLE THRESHOLD: no value refuses every out-of-domain query without also "
            "dropping a relevant document. The floor must stay disabled and the corpus or "
            "reranker revisited — shipping an arbitrary value would trade a visible failure "
            "for a silent one."
        )
    else:
        lines.append(
            f"RECOMMENDED: rag_min_score = {best.threshold} "
            f"(relevance >= {sigmoid(best.threshold):.5f}); "
            f"hit_rate {best.hit_rate:.2f}, refusal_rate {best.refusal_rate:.2f}, "
            f"{best.relevant_dropped} relevant documents lost"
        )
    return "\n".join(lines)


async def main() -> None:
    from core.db import session_scope

    async with session_scope() as session:
        outcomes = await sweep(session)
    print(format_report(outcomes))


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
