"""Agreement scoring across RCA ensemble passes (FR-3.1) — deterministic, unit-tested.

Score = mean over all pass pairs of: 0.6·[same root-cause category] + 0.4·Jaccard(cited
evidence sets). Disagreement therefore shows up whether passes disagree on *what* went
wrong or on *why they think so* — and a low score is surfaced, never averaged away
(PRD 10A edge case).
"""

from __future__ import annotations

from itertools import combinations

from pydantic import BaseModel, Field

UNKNOWN_CATEGORY = "unknown"


class RCAPass(BaseModel):
    """Parsed output of one ensemble pass."""

    root_cause_category: str
    hypothesis: str
    confidence: float = Field(ge=0.0, le=1.0)
    claims: list[dict] = Field(default_factory=list)

    @property
    def cause_identified(self) -> bool:
        """Whether this pass actually named a cause.

        `confidence` qualifies whichever conclusion was reached, and those conclusions are
        not the same kind of thing: "0.9 that a connection pool is exhausted" and "0.9 that
        this evidence cannot identify anything" share a field but not a meaning. Any
        consumer that acts on confidence must check this first — otherwise a future gate of
        the form `confidence > 0.9` fires on an incident the agent explicitly could not
        explain, which is the single most dangerous reading of this number.
        """
        return self.root_cause_category != UNKNOWN_CATEGORY

    @property
    def cited_ids(self) -> set[str]:
        return {c["evidence_id"] for c in self.claims if c.get("evidence_id")}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def agreement_score(passes: list[RCAPass]) -> float:
    """Mean pairwise agreement in [0, 1]; a single pass trivially agrees with itself."""
    if len(passes) < 2:
        return 1.0
    scores = [
        0.6 * (p1.root_cause_category == p2.root_cause_category)
        + 0.4 * _jaccard(p1.cited_ids, p2.cited_ids)
        for p1, p2 in combinations(passes, 2)
    ]
    return round(sum(scores) / len(scores), 4)


def consensus_pass(passes: list[RCAPass]) -> RCAPass:
    """The representative pass: most common category, then highest confidence within it."""
    if not passes:
        raise ValueError("no RCA passes to choose from")
    counts: dict[str, int] = {}
    for p in passes:
        counts[p.root_cause_category] = counts.get(p.root_cause_category, 0) + 1
    top_category = max(counts, key=lambda c: counts[c])
    candidates = [p for p in passes if p.root_cause_category == top_category]
    return max(candidates, key=lambda p: p.confidence)
