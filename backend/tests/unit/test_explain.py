"""Unit tests: agent explainability (ESD §5, §9).

Two things must hold, and they pull in opposite directions.

**It must never be load-bearing.** Explanation runs after the agent's real work and is a
reading aid. Every failure path returns a recorded absence, so an investigation that cannot
explain itself still investigates. A test suite that only covered the happy path would let a
future refactor turn a failed explanation into a failed incident.

**It must not become a second opinion.** The explainer describes what an agent did; it does
not re-decide. A model rendering its own conclusion as "what the agent did" is a fabrication
carrying the authority of an audit record, which is worse than no explanation at all.
"""

from __future__ import annotations

import pytest

from agents.explain import AgentExplanation, explain_step
from api.schemas import AgentStepOut
from providers.base import LLMResult, StructuredResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _explanation(**overrides) -> AgentExplanation:
    base = {
        "headline": "Checkout 5xx traced to payment connection pool exhaustion",
        "what_it_received": "alert plus 6 evidence items",
        "evidence_collected": ["E1 pod logs: UpstreamTimeout"],
        "tools_used": ["k8s.get_pod_logs"],
        "documents_retrieved": ["Runbook: pool exhaustion"],
        "reasoning": "logs show timeouts to payment; metrics confirm 35% 5xx",
        "alternatives_considered": ["OOMKill: rejected, no restarts observed"],
        "confidence": 0.8,
        "uncertainty": "etcd alerts firing concurrently could be a confounder",
        "recommended_next": ["check payment-service pool config"],
    }
    base.update(overrides)
    return AgentExplanation(**base)


class FakeProvider:
    name = "fake"

    def __init__(self, value=None, raises: Exception | None = None):
        self._value = value
        self._raises = raises
        self.calls: list[dict] = []

    async def complete_structured(self, prompt, *, schema, agent, system=None, **kw):
        self.calls.append({"prompt": prompt, "agent": agent, "max_tokens": kw.get("max_tokens")})
        if self._raises:
            raise self._raises
        return StructuredResult(
            value=self._value,
            result=LLMResult(
                text="{}",
                model="fake-model",
                tokens_used=200,
                cost_usd=0.0,
                latency_ms=30,
                prompt_ref=kw.get("prompt_ref"),
            ),
        )


async def _explain(provider, **overrides):
    kwargs = {
        "agent": "rca",
        "title": "Checkout 5xx spike",
        "service": "checkout-service",
        "inputs": "alert + evidence",
        "evidence": ["E1: UpstreamTimeout"],
        "tools_used": ["k8s.get_pod_logs"],
        "retrieved_docs": ["Runbook: pool exhaustion"],
        "output": "hypothesis: pool exhaustion",
    }
    kwargs.update(overrides)
    return await explain_step(provider, **kwargs)


# --- the happy path ------------------------------------------------------------------------


async def test_explanation_is_returned_with_accounting():
    outcome = await _explain(FakeProvider(_explanation()))
    assert outcome.explanation is not None
    assert outcome.explanation.headline.startswith("Checkout 5xx")
    assert outcome.model_used == "fake-model"
    assert outcome.tokens_used == 200
    assert outcome.degraded is False


async def test_explanation_is_token_capped():
    """A long explanation is a failed explanation; the cap is part of the design."""
    provider = FakeProvider(_explanation())
    await _explain(provider)
    assert provider.calls[0]["max_tokens"] <= 1000


async def test_uncertainty_is_a_required_field():
    """Making it structurally required is what stops explanations reading as uniformly
    confident — an omitted uncertainty would otherwise silently default to empty."""
    assert AgentExplanation.model_fields["uncertainty"].is_required()


# --- never load-bearing ----------------------------------------------------------------------


async def test_model_failure_degrades_instead_of_raising():
    outcome = await _explain(FakeProvider(raises=RuntimeError("upstream down")))
    assert outcome.explanation is None
    assert outcome.degraded is True
    assert "unavailable" in outcome.reason


async def test_no_provider_degrades_quietly():
    outcome = await _explain(None)
    assert outcome.explanation is None
    assert outcome.degraded is True


@pytest.mark.parametrize("bad", [ValueError("x"), TimeoutError(), KeyError("k")])
async def test_no_exception_type_escapes(bad):
    """The caller persists a step immediately after this; anything that escapes here would
    lose the agent's actual work to a failure in describing it."""
    outcome = await _explain(FakeProvider(raises=bad))
    assert outcome.explanation is None


async def test_empty_inputs_do_not_crash_rendering():
    outcome = await _explain(
        FakeProvider(_explanation()), evidence=None, tools_used=None, retrieved_docs=None
    )
    assert outcome.explanation is not None


# --- the API contract ------------------------------------------------------------------------


def test_api_lifts_explanation_out_of_structured_output():
    """The frontend reads a typed field, not a JSON blob."""
    import datetime
    import uuid

    step = AgentStepOut(
        id=uuid.uuid4(),
        agent_name="rca",
        ensemble_pass_index=None,
        input_summary=None,
        output_summary="hypothesis",
        structured_output={"explanation": _explanation().model_dump(mode="json")},
        confidence=0.8,
        model_used="gemini-3.1-flash-lite",
        tokens_used=100,
        cost_usd=0.0,
        latency_ms=10,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    assert step.explanation is not None
    assert step.explanation.tools_used == ["k8s.get_pod_logs"]


def test_malformed_explanation_is_dropped_not_raised():
    """A bad explanation must not make the whole incident unreadable."""
    import datetime
    import uuid

    step = AgentStepOut(
        id=uuid.uuid4(),
        agent_name="rca",
        ensemble_pass_index=None,
        input_summary=None,
        output_summary="hypothesis",
        structured_output={"explanation": {"confidence": "not-a-number"}},
        confidence=None,
        model_used=None,
        tokens_used=None,
        cost_usd=None,
        latency_ms=None,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    assert step.explanation is None


def test_step_without_explanation_is_valid():
    import datetime
    import uuid

    step = AgentStepOut(
        id=uuid.uuid4(),
        agent_name="orchestrator",
        ensemble_pass_index=None,
        input_summary=None,
        output_summary="routed",
        structured_output={"next_step": "rca"},
        confidence=None,
        model_used=None,
        tokens_used=None,
        cost_usd=None,
        latency_ms=None,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    assert step.explanation is None
