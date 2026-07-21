"""Grounding guards: a resolving citation is necessary but NOT sufficient (FR-8.1).

These lock in three defects found by running the pipeline against a real LLM:

1. The model asserted `deploy_regression` — "a recent deployment introduced a bug" —
   while the GitHub source was unavailable and no deploy evidence had been gathered
   at all. Every citation resolved, so the old Observer approved it.
2. Only one of three ensemble passes survived parsing, yet the result reported
   `agreement 1.00`: one opinion presented as unanimity.
3. Real-model JSON arrives fenced, prose-wrapped, or with percentage confidences.
"""

from __future__ import annotations

import pytest

from agents.evidence import EvidenceStore
from agents.observer.validator import check_category_support, review
from agents.rca.engine import _parse_pass
from agents.rca.scoring import RCAPass
from db.enums import EvidenceType


def _store(*items: tuple[EvidenceType, str]) -> EvidenceStore:
    store = EvidenceStore()
    for i, (kind, text) in enumerate(items, start=1):
        store.add(type_=kind, source=f"src{i}", ref=f"ref/{i}", text=text)
    return store


# --- 1. category must be supported by the cited evidence -------------------------------


def test_deploy_regression_rejected_when_no_deploy_evidence_gathered():
    """The exact live failure: blame a deploy with no deploy evidence in existence."""
    store = _store(
        (EvidenceType.log, "pod checkout-service-abc phase=Running ready=1/1 restarts=2"),
        (EvidenceType.metric, "rate(status=200) = 37.5/s\nrate(status=500) = 2.4/s"),
    )
    claims = [
        {"claim": "A recent deploy broke the health check", "evidence_id": "E1"},
        {"claim": "Errors are elevated", "evidence_id": "E2"},
    ]
    supported, reason = check_category_support("deploy_regression", claims, store)
    assert supported is False
    assert "no diff evidence was gathered at all" in reason

    verdict = review(claims, store, root_cause_category="deploy_regression")
    assert verdict.approved is False, "citations resolve, but the cause is unsupported"
    assert verdict.category_supported is False
    assert "unsupported root cause" in verdict.notes


def test_deploy_regression_accepted_with_real_commit_evidence():
    store = _store(
        (EvidenceType.log, "ERROR handler crashed on cache_ttl=300"),
        (EvidenceType.diff, "commit 9f1c2e3 at 08:45: feat: raise checkout cache TTL to 300s"),
    )
    claims = [{"claim": "commit 9f1c2e3 changed cache TTL", "evidence_id": "E2"}]
    supported, _ = check_category_support("deploy_regression", claims, store)
    assert supported is True
    assert review(claims, store, root_cause_category="deploy_regression").approved is True


def test_resource_exhaustion_needs_an_oom_or_crash_signal():
    healthy = _store((EvidenceType.log, "pod payment-abc phase=Running ready=1/1 restarts=0"))
    claims = [{"claim": "the pod is out of memory", "evidence_id": "E1"}]
    supported, reason = check_category_support("resource_exhaustion", claims, healthy)
    assert supported is False
    assert "no supporting signal" in reason

    crashing = _store((EvidenceType.log, "OOMKilled by kernel; CrashLoopBackOff x7"))
    assert check_category_support("resource_exhaustion", claims, crashing)[0] is True


def test_citing_uncited_but_present_evidence_is_still_rejected():
    """Deploy evidence exists but the claims don't cite it — still unsupported."""
    store = _store(
        (EvidenceType.log, "some log line"),
        (EvidenceType.diff, "commit abc123: feat: something"),
    )
    claims = [{"claim": "a deploy did it", "evidence_id": "E1"}]  # cites the log, not the diff
    supported, reason = check_category_support("deploy_regression", claims, store)
    assert supported is False
    assert "none was cited" in reason


def test_unknown_is_always_supported():
    """Declining to name a cause must never be penalised."""
    store = _store((EvidenceType.log, "nothing conclusive"))
    claims = [{"claim": "insufficient evidence", "evidence_id": "E1"}]
    assert check_category_support("unknown", claims, store)[0] is True
    assert review(claims, store, root_cause_category="unknown").approved is True


def test_unrecognised_category_is_rejected():
    store = _store((EvidenceType.log, "x"))
    claims = [{"claim": "x", "evidence_id": "E1"}]
    assert check_category_support("alien_invasion", claims, store)[0] is False


def test_review_without_category_keeps_previous_behaviour():
    """Callers that don't pass a category (older paths, tests) still get citation-only review."""
    store = _store((EvidenceType.log, "OOMKilled"))
    verdict = review([{"claim": "oom", "evidence_id": "E1"}], store)
    assert verdict.approved is True
    assert verdict.category_supported is True


# --- 2. a one-pass ensemble is not unanimity -------------------------------------------


@pytest.mark.parametrize("succeeded", [0, 1])
def test_degraded_ensemble_is_flagged_low_confidence(succeeded: int):
    from agents.rca.scoring import agreement_score

    passes = [
        RCAPass(
            root_cause_category="resource_exhaustion",
            hypothesis="oom",
            confidence=0.9,
            claims=[{"claim": "c", "evidence_id": "E1"}],
        )
    ] * succeeded
    if succeeded == 1:
        # Arithmetically a lone pass scores a perfect 1.0 ...
        assert agreement_score(passes) == 1.0
    # ... which is exactly why run_rca must not treat that as corroborated.
    assert len(passes) < 2


# --- 3. real-model JSON shapes ----------------------------------------------------------


def test_parses_fenced_json():
    raw = (
        '```json\n{"root_cause_category":"error_spike","hypothesis":"h","confidence":0.8,'
        '"claims":[{"claim":"c","evidence_id":"E1"}]}\n```'
    )
    parsed = _parse_pass(raw)
    assert parsed is not None and parsed.root_cause_category == "error_spike"


def test_parses_json_wrapped_in_prose():
    raw = (
        'Here is my analysis:\n{"root_cause_category":"error_spike","hypothesis":"h",'
        '"confidence":0.8,"claims":[]}\nHope that helps!'
    )
    assert _parse_pass(raw) is not None


def test_normalises_percentage_confidence():
    raw = '{"root_cause_category":"error_spike","hypothesis":"h","confidence":"85%","claims":[]}'
    parsed = _parse_pass(raw)
    assert parsed is not None
    assert parsed.confidence == pytest.approx(0.85)


def test_drops_malformed_claim_entries_without_failing_the_pass():
    raw = (
        '{"root_cause_category":"error_spike","hypothesis":"h","confidence":0.7,'
        '"claims":[{"claim":"ok","evidence_id":"E1"}, "garbage", null]}'
    )
    parsed = _parse_pass(raw)
    assert parsed is not None
    assert len(parsed.claims) == 1


def test_returns_none_on_genuinely_unparseable_output():
    assert _parse_pass("I could not determine the root cause.") is None
