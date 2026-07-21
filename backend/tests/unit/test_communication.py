"""Unit tests: Communication Agent templates — plain English, all FR-6.2 phases."""

from __future__ import annotations

import pytest

from agents.communication.composer import _TEMPLATES, compose

JARGON = ["pod", "k8s", "kubernetes", "5xx", "rca", "p99", "oomkilled", "replica", "deployment"]


@pytest.mark.parametrize("phase", list(_TEMPLATES))
def test_every_phase_renders_plain_english(phase: str):
    text = compose(
        phase,
        service="checkout-service",
        severity="P1",
        root_cause_category="resource_exhaustion",
        action_type="restart_pod",
    )
    assert "checkout-service" in text
    lowered = text.lower()
    for term in JARGON:
        assert term not in lowered, f"jargon '{term}' leaked into a stakeholder update"


def test_fr62_transition_points_are_covered():
    # FR-6.2: opened, root cause identified, remediation proposed/executed, resolved.
    assert {
        "opened",
        "root_cause",
        "remediation_proposed",
        "remediation_executed",
        "resolved",
    } <= set(_TEMPLATES)


def test_root_cause_update_mentions_next_step_when_action_exists():
    text = compose(
        "root_cause",
        service="payment-service",
        severity="P1",
        root_cause_category="latency_degradation",
        action_type="scale_deployment",
    )
    assert "Next step" in text
    no_action = compose(
        "root_cause",
        service="payment-service",
        severity="P1",
        root_cause_category="error_spike",
        action_type=None,
    )
    assert "engineer is reviewing" in no_action
