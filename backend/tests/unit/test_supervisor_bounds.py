"""Unit tests: the LLM supervisor's enforcement boundary (ESD §15, §24).

The supervisor supplies judgment; Python supplies the guarantees. Every test here asserts a
bound the model **cannot** talk its way past, because each was either a real loop observed in
development or a spend risk a prompt injection could otherwise trigger.
"""

from __future__ import annotations

from core.config import get_settings
from orchestrator.supervisor import RoutingDecision, available_steps, decide
from providers.base import LLMResult, StructuredResult


class _Store:
    def __init__(self, items=(), gaps=()):
        self.items = list(items)
        self.gaps = list(gaps)


class _Verdict:
    def __init__(self, approved: bool, notes: str = "") -> None:
        self.approved = approved
        self.notes = notes


class _Chooses:
    """Provider double that always requests one specific step."""

    name = "chooses"

    def __init__(self, step: str) -> None:
        self._step = step

    async def complete_structured(self, prompt, *, schema, agent, **kwargs):
        return StructuredResult(
            value=RoutingDecision(next_step=self._step, reasoning="because"),
            result=LLMResult(text="", model="d", tokens_used=1, cost_usd=0.0, latency_ms=1),
        )


class _Broken:
    name = "broken"

    async def complete_structured(self, *a, **k):
        raise RuntimeError("no capacity")


# --- availability is state-driven ---------------------------------------------------------


def test_correlation_runs_before_anything_else():
    assert available_steps({}) == ["correlation"]


def test_correlation_that_gathered_nothing_still_advances_to_rca():
    """Regression: gating on evidence looped forever when every source was down."""
    state = {"completed_steps": ["correlation"], "evidence_store": _Store(gaps=["k8s: down"])}
    assert available_steps(state) == ["rca"]


def test_observer_follows_rca():
    state = {"completed_steps": ["correlation", "rca"], "rca_result": object()}
    assert available_steps(state) == ["observer"]


def test_approved_hypothesis_offers_resolution_then_finalize():
    state = {
        "completed_steps": ["correlation", "rca", "observer"],
        "rca_result": object(),
        "observer_verdict": _Verdict(True),
    }
    assert available_steps(state) == ["resolution", "finalize"]


def test_resolution_is_not_offered_twice():
    state = {
        "completed_steps": ["correlation", "rca", "observer", "resolution"],
        "rca_result": object(),
        "observer_verdict": _Verdict(True),
    }
    assert available_steps(state) == ["finalize"]


# --- the bounds the model cannot exceed ---------------------------------------------------


def test_revision_limit_removes_revise_from_the_options():
    settings = get_settings()
    state = {
        "completed_steps": ["correlation", "rca", "observer"],
        "rca_result": object(),
        "observer_verdict": _Verdict(False, "rejected"),
        "revision_count": settings.supervisor_max_revisions,
    }
    assert "revise" not in available_steps(state)


def test_correlation_reinvocation_is_capped():
    """'Gather more evidence' is always plausible, so it needs a hard cap."""
    settings = get_settings()
    state = {
        "completed_steps": ["correlation"] * settings.correlation_max_invocations + ["rca"],
        "rca_result": object(),
        "observer_verdict": _Verdict(False, "rejected"),
        "revision_count": 0,
    }
    assert "correlation" not in available_steps(state)


def test_token_budget_exhaustion_forces_finalize():
    state = {
        "completed_steps": ["correlation", "rca", "observer"],
        "rca_result": object(),
        "observer_verdict": _Verdict(True),
        "tokens_used": get_settings().incident_token_budget,
    }
    assert available_steps(state) == ["finalize"]


def test_finalize_or_escalate_always_remains_reachable():
    """There must never be a state whose only options continue the investigation."""
    state = {
        "completed_steps": ["correlation", "rca", "observer"],
        "rca_result": object(),
        "observer_verdict": _Verdict(False, "rejected"),
        "revision_count": 99,
    }
    assert {"finalize", "escalate"} & set(available_steps(state))


# --- the model's choice is validated ------------------------------------------------------


async def test_illegal_choice_is_overridden_not_honoured():
    """The core guarantee: a model asking for a forbidden step does not get it."""
    state = {
        "completed_steps": ["correlation", "rca", "observer"],
        "rca_result": object(),
        "observer_verdict": _Verdict(False, "rejected"),
        "revision_count": get_settings().supervisor_max_revisions,  # revise is illegal
    }
    outcome = await decide(_Chooses("revise"), state)
    assert outcome.step != "revise"
    assert outcome.overridden_from == "revise"
    assert outcome.llm_decided is False


async def test_legal_choice_is_honoured():
    state = {
        "completed_steps": ["correlation", "rca", "observer"],
        "rca_result": object(),
        "observer_verdict": _Verdict(True),
    }
    outcome = await decide(_Chooses("finalize"), state)
    assert outcome.step == "finalize"
    assert outcome.llm_decided is True


async def test_single_option_skips_the_model_call():
    """No decision to make — spending a model call to prove it is waste during an outage."""
    outcome = await decide(_Broken(), {})
    assert outcome.step == "correlation"
    assert outcome.llm_decided is False


async def test_unavailable_supervisor_falls_back_deterministically():
    state = {
        "completed_steps": ["correlation", "rca", "observer"],
        "rca_result": object(),
        "observer_verdict": _Verdict(True),
    }
    outcome = await decide(_Broken(), state)
    assert outcome.step in available_steps(state)
    assert outcome.llm_decided is False
