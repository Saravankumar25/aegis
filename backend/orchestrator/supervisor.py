"""LLM supervisor: decides which step runs next (ESD §24, Supervisor pattern).

The graph previously encoded routing as static edges with a single boolean conditional, which
made "Supervisor pattern" a description of the file layout rather than of any decision being
made. This module makes the routing decision real: the model reads the shared state and
chooses the next step.

**Its authority is bounded, and the bounds are enforced in Python, not requested in the
prompt.** `decide()` computes the set of *legally available* steps from the state, and a
choice outside that set is rejected and replaced by the deterministic fallback. So the model
can decide the investigation needs another RCA pass, but it cannot:

* exceed the revision limit (bounded work per incident, ESD §15);
* exceed the incident token budget;
* route to `resolution` without an observer-validated hypothesis;
* loop forever by never choosing to finalize.

This is the same principle as the Triage clamp: the model supplies judgment, Python supplies
the guarantees. A supervisor that could talk itself past the revision limit would turn a
prompt injection in a pod log into an unbounded spend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.prompts.library import SUPERVISOR_ROUTE
from core.config import get_settings
from core.logging import get_logger
from guardrails import guard_input

_log = get_logger(component="orchestrator.supervisor")

Step = Literal["correlation", "rca", "observer", "revise", "resolution", "finalize", "escalate"]

_STEP_DESCRIPTIONS: dict[str, str] = {
    "correlation": "Gather more evidence from infrastructure.",
    "rca": "Form (or re-form) a root-cause hypothesis from the evidence.",
    "observer": "Validate the current hypothesis against its cited evidence.",
    "revise": "Drop injection-flagged evidence and re-run RCA once.",
    "resolution": "Propose a remediation for a validated hypothesis.",
    "finalize": "Conclude the investigation with what is currently known.",
    "escalate": "Hand to a human: the evidence cannot support a conclusion.",
}


class RoutingDecision(BaseModel):
    """The supervisor's choice."""

    next_step: Step = Field(description="Exactly one step name from the catalog.")
    reasoning: str = Field(
        description="Why this step, referring to the investigation state.", max_length=600
    )
    confidence_sufficient: bool = Field(
        default=False,
        description="Whether the current hypothesis is strong enough to act on.",
    )


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    step: str
    reasoning: str
    llm_decided: bool
    overridden_from: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    prompt_ref: str | None = None
    model_used: str | None = None
    latency_ms: int | None = None


def available_steps(state: dict[str, Any]) -> list[str]:
    """Steps that are legal given the current state.

    This is the enforcement boundary. Anything absent from this list cannot be chosen, no
    matter what the model returns.
    """
    settings = get_settings()
    completed = state.get("completed_steps", [])
    result = state.get("rca_result")
    verdict = state.get("observer_verdict")
    approved = bool(verdict and getattr(verdict, "approved", False))
    revisions = state.get("revision_count", 0)
    over_budget = state.get("tokens_used", 0) >= settings.incident_token_budget

    # Budget exhaustion ends the investigation regardless of what the model would prefer.
    if over_budget:
        return ["finalize"]

    steps: list[str] = []
    # Gated on whether correlation has *run*, not on whether it produced anything. Gating on
    # evidence looped forever when every source was down: correlation would be the only legal
    # step, produce nothing but gaps, and be chosen again. RCA is reachable with an empty
    # store on purpose — it answers "unknown" from documented gaps, which is the correct
    # result for an incident whose evidence sources are all unavailable.
    if "correlation" not in completed:
        return ["correlation"]
    if result is None:
        return ["rca"]
    if verdict is None:
        return ["observer"]

    if approved:
        # Offered only once: resolution is idempotent at the action layer, but re-routing to
        # it would spin the supervisor cycle without producing anything new.
        if "resolution" not in state.get("completed_steps", []):
            steps.append("resolution")
        steps.append("finalize")
    else:
        # A rejected hypothesis may be revised only while revisions remain.
        if revisions < settings.supervisor_max_revisions:
            steps.append("revise")
            # Re-gathering is capped independently of revisions. Without this cap the
            # supervisor can sit in a supervisor→correlation cycle forever: "gather more
            # evidence" is always a *plausible* next step, so a model that likes it is never
            # forced to conclude. Observed with a stub that always picked the first option;
            # a real model under an ambiguous incident can do the same thing.
            if completed.count("correlation") < settings.correlation_max_invocations:
                steps.append("correlation")
        steps.extend(["finalize", "escalate"])
    return steps


def _deterministic_fallback(state: dict[str, Any], allowed: list[str]) -> str:
    """The routing the graph used before, used when the model cannot or must not decide."""
    verdict = state.get("observer_verdict")
    approved = bool(verdict and getattr(verdict, "approved", False))
    if approved and "resolution" in allowed:
        return "resolution"
    if "revise" in allowed:
        return "revise"
    return allowed[0]


async def decide(provider: Any, state: dict[str, Any]) -> RoutingOutcome:
    """Choose the next step. Falls back to deterministic routing on any failure."""
    allowed = available_steps(state)
    if len(allowed) == 1:
        # No decision to make — do not spend a model call proving it.
        return RoutingOutcome(
            step=allowed[0],
            reasoning="only one legal next step given the investigation state",
            llm_decided=False,
        )

    settings = get_settings()
    result = state.get("rca_result")
    verdict = state.get("observer_verdict")
    store = state.get("evidence_store")

    prompt = SUPERVISOR_ROUTE.render(
        title=state.get("title", ""),
        service=state.get("service_name", ""),
        severity=state.get("severity", ""),
        completed=", ".join(state.get("completed_steps", [])) or "none",
        evidence_count=len(getattr(store, "items", []) or []),
        gap_count=len(getattr(store, "gaps", []) or []),
        hypothesis=getattr(result, "hypothesis", None) or "none yet",
        observer_verdict=(
            "approved"
            if verdict and getattr(verdict, "approved", False)
            else (getattr(verdict, "notes", None) or "not yet reviewed")
        ),
        revision_count=state.get("revision_count", 0),
        max_revisions=settings.supervisor_max_revisions,
        tokens_used=state.get("tokens_used", 0),
        token_budget=settings.incident_token_budget,
        step_catalog="\n".join(f"- {s}: {_STEP_DESCRIPTIONS[s]}" for s in allowed),
    )
    guarded = guard_input(prompt, agent="supervisor")

    try:
        structured = await provider.complete_structured(
            guarded.prompt,
            schema=RoutingDecision,
            agent="supervisor",
            system=SUPERVISOR_ROUTE.system,
            prompt_ref=SUPERVISOR_ROUTE.ref,
        )
    except Exception as exc:  # noqa: BLE001 — routing must never stall the investigation
        _log.warning("supervisor_llm_unavailable", error=str(exc))
        return RoutingOutcome(
            step=_deterministic_fallback(state, allowed),
            reasoning=f"deterministic routing; supervisor model unavailable ({type(exc).__name__})",
            llm_decided=False,
        )

    decision = structured.value
    chosen = decision.next_step

    if chosen not in allowed:
        # The model asked for something the state does not permit — most importantly, another
        # revision past the limit. Enforced here rather than trusted to the prompt.
        fallback = _deterministic_fallback(state, allowed)
        _log.warning(
            "supervisor_choice_overridden",
            requested=chosen,
            allowed=allowed,
            used=fallback,
        )
        return RoutingOutcome(
            step=fallback,
            reasoning=f"model chose '{chosen}', which is not permitted in this state; "
            f"routed to '{fallback}'",
            llm_decided=False,
            overridden_from=chosen,
            tokens_used=structured.result.tokens_used,
            cost_usd=structured.result.cost_usd,
            prompt_ref=structured.result.prompt_ref,
            model_used=structured.result.model,
            latency_ms=structured.result.latency_ms,
        )

    _log.info("supervisor_route", step=chosen, reasoning=decision.reasoning[:200])
    return RoutingOutcome(
        step=chosen,
        reasoning=decision.reasoning,
        llm_decided=True,
        tokens_used=structured.result.tokens_used,
        cost_usd=structured.result.cost_usd,
        prompt_ref=structured.result.prompt_ref,
        model_used=structured.result.model,
        latency_ms=structured.result.latency_ms,
    )
