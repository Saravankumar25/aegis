"""Eval harness: RCA accuracy + hallucination rate over the synthetic corpus (ESD §22).

Runs the real correlation → RCA → observer path (no DB, FixtureGateway + stub provider)
over every scenario in eval/synthetic_incidents/ and enforces the PRD 9A success metrics:
accuracy ≥ 85 %, hallucination (claims whose citation does not resolve) < 5 %. Runs in CI
on every PR — token-free by design.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.correlation.collector import collect_evidence
from agents.gateway import FixtureGateway
from agents.observer.validator import review, validate_claims
from agents.rca.engine import run_rca
from providers.stub import StubProvider

SCENARIO_DIR = Path(__file__).resolve().parents[3] / "eval" / "synthetic_incidents"

ACCURACY_THRESHOLD = 0.85  # PRD 9A
HALLUCINATION_THRESHOLD = 0.05  # PRD 9A


def _load_scenarios() -> list[dict]:
    scenarios = [
        json.loads(p.read_text(encoding="utf-8")) for p in sorted(SCENARIO_DIR.glob("*.json"))
    ]
    assert len(scenarios) >= 10, "corpus shrank — keep at least 10 scenarios"
    return scenarios


def _gateway(scenario: dict) -> FixtureGateway:
    fixtures = {
        tuple(key.split("::", 1)): value for key, value in scenario["fixtures"].items()
    }
    return FixtureGateway(fixtures)


async def _run(scenario: dict) -> dict:
    store, _summary = await collect_evidence(_gateway(scenario), scenario["service_name"])
    result = await run_rca(
        StubProvider(),
        service=scenario["service_name"],
        title=scenario["title"],
        store=store,
        runbook_context="",
    )
    verdict = review(result.claims, store)
    # One observer-driven revision, mirroring the orchestrator's bounded loop.
    if not verdict.approved and verdict.flagged_evidence:
        poisoned = {f["evidence_id"] for f in verdict.flagged_evidence}
        store.items = [i for i in store.items if i.id not in poisoned]
        result = await run_rca(
            StubProvider(),
            service=scenario["service_name"],
            title=scenario["title"],
            store=store,
            runbook_context="",
        )
    claim_verdicts = validate_claims(result.claims, store)
    return {
        "name": scenario["name"],
        "expected": scenario["ground_truth_category"],
        "got": result.root_cause_category,
        "correct": result.root_cause_category == scenario["ground_truth_category"],
        "claims": len(claim_verdicts),
        "hallucinated": sum(1 for v in claim_verdicts if not v.valid),
        "agreement": result.agreement_score,
    }


async def test_rca_accuracy_meets_threshold():
    rows = [await _run(s) for s in _load_scenarios()]
    accuracy = sum(r["correct"] for r in rows) / len(rows)
    misses = [f"{r['name']}: expected {r['expected']}, got {r['got']}" for r in rows
              if not r["correct"]]
    assert accuracy >= ACCURACY_THRESHOLD, (
        f"RCA accuracy {accuracy:.0%} below {ACCURACY_THRESHOLD:.0%}; misses: {misses}"
    )


async def test_hallucination_rate_below_threshold():
    rows = [await _run(s) for s in _load_scenarios()]
    total_claims = sum(r["claims"] for r in rows)
    hallucinated = sum(r["hallucinated"] for r in rows)
    assert total_claims > 0
    rate = hallucinated / total_claims
    assert rate < HALLUCINATION_THRESHOLD, (
        f"hallucination rate {rate:.1%} ≥ {HALLUCINATION_THRESHOLD:.0%}"
    )


@pytest.mark.parametrize("scenario", _load_scenarios(), ids=lambda s: s["name"])
async def test_each_scenario_produces_cited_hypothesis(scenario: dict):
    """Every scenario must yield a hypothesis with ≥1 resolving citation (FR-3.2)."""
    row = await _run(scenario)
    assert row["claims"] >= 1
    assert row["hallucinated"] == 0
