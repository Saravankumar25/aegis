"""Unit tests: prompt registry contract and the Triage severity clamp.

The clamp is the safety-relevant part. Triage now takes a model's judgment, and the rule
that a model may **raise** severity but never **lower** it is what stops a prompt-injected
alert title from downgrading a P1 and suppressing the page.
"""

from __future__ import annotations

import pytest

from agents.prompts import REGISTRY, Prompt, PromptRenderError
from agents.prompts.library import RCA_HYPOTHESIS, TRIAGE_SEVERITY
from agents.triage.reasoner import TriageJudgment, assess
from db.enums import Severity
from providers.base import LLMResult, StructuredResult

# --- prompt registry ----------------------------------------------------------------------


def test_prompt_ref_identifies_id_version_and_content():
    ref = TRIAGE_SEVERITY.ref
    assert ref.startswith("triage.severity@1.0.0+")
    assert len(ref.split("+")[1]) == 12


def test_fingerprint_changes_when_content_changes():
    """A template edited without a version bump must not be silently indistinguishable."""
    base = Prompt(id="t", version="1.0.0", system="s", template="hello {name}")
    edited = Prompt(id="t", version="1.0.0", system="s", template="HELLO {name}")
    assert base.fingerprint != edited.fingerprint


def test_missing_variable_raises_rather_than_rendering_a_placeholder():
    """A literal '{service}' reaching a model yields a plausible answer to a broken question."""
    with pytest.raises(PromptRenderError) as exc:
        TRIAGE_SEVERITY.render(title="t")
    assert "missing required variable" in str(exc.value)


def test_render_succeeds_with_the_full_contract():
    rendered = TRIAGE_SEVERITY.render(
        title="5xx spike",
        service="checkout-service",
        kind="error_rate",
        value=0.4,
        service_role="revenue path",
        dependents="none",
        floor_severity="P1",
    )
    assert "checkout-service" in rendered
    assert "{" not in rendered.replace("{", "", 0) or "{service}" not in rendered


def test_registry_rejects_duplicate_ids():
    with pytest.raises(ValueError):
        REGISTRY.register(Prompt(id="triage.severity", version="9", system="", template=""))


def test_every_registered_prompt_has_a_system_message():
    """The system role carries the injection-resistance contract; an empty one drops it."""
    for prompt in REGISTRY.all():
        assert prompt.system.strip(), f"{prompt.id} has no system message"


def test_rca_prompt_still_forbids_uncited_claims():
    """Guards the grounding contract against a careless prompt edit."""
    assert "MUST cite" in RCA_HYPOTHESIS.template
    assert "unknown" in RCA_HYPOTHESIS.template


# --- Triage clamp -------------------------------------------------------------------------


class _FixedJudgment:
    """Provider double returning one chosen severity."""

    name = "fixed"

    def __init__(self, severity: str) -> None:
        self._severity = severity

    async def complete_structured(self, prompt, *, schema, agent, **kwargs):
        value = TriageJudgment(
            severity=self._severity,
            customer_impact="impact",
            reasoning="reasoning",
        )
        return StructuredResult(
            value=value,
            result=LLMResult(text="", model="fixed", tokens_used=1, cost_usd=0.0, latency_ms=1),
        )


class _BrokenProvider:
    name = "broken"

    async def complete_structured(self, *args, **kwargs):
        raise RuntimeError("no capacity")


async def test_model_may_escalate_above_the_floor():
    # catalog-service + latency has a P3 floor; a model may argue it is worse.
    outcome = await assess(
        _FixedJudgment("P1"), service="catalog-service", kind="latency", title="latency"
    )
    assert outcome.severity is Severity.P1
    assert outcome.escalated is True
    assert outcome.clamped is False


async def test_model_may_not_lower_severity_below_the_floor():
    """The load-bearing safety property: de-escalation is an attack surface."""
    # checkout-service + error_rate has a P1 floor.
    outcome = await assess(
        _FixedJudgment("P4"), service="checkout-service", kind="error_rate", title="5xx spike"
    )
    assert outcome.severity is Severity.P1
    assert outcome.clamped is True
    assert outcome.model_severity is Severity.P4


async def test_agreement_with_the_floor_is_neither_escalation_nor_clamp():
    outcome = await assess(
        _FixedJudgment("P1"), service="checkout-service", kind="error_rate", title="5xx"
    )
    assert outcome.severity is Severity.P1
    assert outcome.escalated is False
    assert outcome.clamped is False


async def test_unavailable_model_degrades_to_the_floor():
    """Triage is on the critical path; a throttled model must not stall the incident."""
    outcome = await assess(
        _BrokenProvider(), service="checkout-service", kind="error_rate", title="5xx"
    )
    assert outcome.severity is Severity.P1
    assert outcome.degraded is True
    assert outcome.model_severity is None
