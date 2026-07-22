"""Unit tests: ensemble agreement scoring + consensus selection (FR-3.1)."""

from __future__ import annotations

import pytest

from agents.rca.scoring import RCAPass, agreement_score, consensus_pass


def _p(category: str, confidence: float, *evidence_ids: str) -> RCAPass:
    return RCAPass(
        root_cause_category=category,
        hypothesis=f"{category} hypothesis",
        confidence=confidence,
        claims=[{"claim": "c", "evidence_id": e} for e in evidence_ids],
    )


def test_unanimous_passes_score_one():
    passes = [_p("deploy_regression", 0.8, "E1", "E2")] * 3
    assert agreement_score(passes) == 1.0


def test_total_disagreement_scores_zero():
    passes = [
        _p("deploy_regression", 0.8, "E1"),
        _p("resource_exhaustion", 0.7, "E2"),
        _p("latency_degradation", 0.6, "E3"),
    ]
    assert agreement_score(passes) == 0.0


def test_partial_agreement_is_between():
    passes = [
        _p("deploy_regression", 0.8, "E1", "E2"),
        _p("deploy_regression", 0.7, "E2", "E3"),
        _p("resource_exhaustion", 0.6, "E2"),
    ]
    score = agreement_score(passes)
    assert 0.0 < score < 1.0


def test_single_pass_trivially_agrees():
    assert agreement_score([_p("deploy_regression", 0.8, "E1")]) == 1.0


def test_consensus_prefers_majority_category_then_confidence():
    passes = [
        _p("deploy_regression", 0.7, "E1"),
        _p("deploy_regression", 0.9, "E1", "E2"),
        _p("resource_exhaustion", 0.99, "E3"),
    ]
    best = consensus_pass(passes)
    assert best.root_cause_category == "deploy_regression"
    assert best.confidence == 0.9


def test_empty_and_no_citation_jaccard_edge():
    # Two passes agreeing on category with no citations at all: category term only.
    passes = [_p("deploy_regression", 0.5), _p("deploy_regression", 0.5)]
    assert agreement_score(passes) == 1.0  # 0.6 category + 0.4 empty-set Jaccard(=1)


# --- "unknown" is a conclusion, not a missing value ----------------------------------------


def test_unknown_at_high_confidence_is_not_an_identified_cause():
    """The ambiguity this property exists to remove.

    `confidence` qualifies whichever conclusion was reached, and "unknown" is a conclusion.
    So `unknown` at 0.95 means "confident this evidence identifies nothing" — which reads
    identically to a confident diagnosis if a caller only looks at the number. A future gate
    of the form `confidence > 0.9` would fire on an incident the agent explicitly could not
    explain.
    """
    from agents.rca.engine import RCAResult

    result = RCAResult(
        hypothesis="the evidence does not identify a cause",
        root_cause_category="unknown",
        confidence=0.95,
        agreement_score=1.0,
        low_confidence=False,
    )
    assert result.confidence > 0.9
    assert result.cause_identified is False, (
        "high confidence in 'unknown' must never read as an identified cause"
    )


def test_a_named_category_is_an_identified_cause():
    from agents.rca.engine import RCAResult

    result = RCAResult(
        hypothesis="connection pool exhausted",
        root_cause_category="resource_exhaustion",
        confidence=0.9,
        agreement_score=1.0,
        low_confidence=False,
    )
    assert result.cause_identified is True


@pytest.mark.parametrize(
    "category", ["resource_exhaustion", "latency_degradation", "deploy_regression", "error_spike"]
)
def test_every_real_category_counts_as_identified(category):
    assert RCAPass(root_cause_category=category, hypothesis="x", confidence=0.5).cause_identified


def test_pass_level_unknown_is_also_not_identified():
    """The same distinction has to hold per-pass, since consensus is chosen from passes."""
    assert not RCAPass(
        root_cause_category="unknown", hypothesis="x", confidence=1.0
    ).cause_identified
