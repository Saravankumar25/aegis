"""Unit tests: ensemble agreement scoring + consensus selection (FR-3.1)."""

from __future__ import annotations

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
