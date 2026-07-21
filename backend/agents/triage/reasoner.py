"""Triage Agent: LLM severity judgment over a deterministic floor (FR-1.3).

Severity was previously a two-`set` lookup — real logic, but not judgment. It could not read
an alert saying "checkout succeeding for 3% of users" any differently from one saying
"checkout p99 up 40ms", because it only ever looked at the service name and a coarse `kind`.
That is the assessment this module hands to a model.

**The rule-based classifier stays, as a floor.** Two reasons, and both are load-bearing:

* *Latency.* PRD 11A requires an incident row with a severity within one second of the
  webhook. An LLM call cannot sit on that path, so ingestion keeps using the deterministic
  classifier and this agent refines the value afterwards.
* *Safety.* The floor encodes revenue-path knowledge that is not visible in the alert text —
  that `checkout-service` and `payment-service` are the money path. A model reading only an
  alert cannot know that, and a prompt-injected log line must never be able to talk Aegis
  into downgrading a P1 to P4 and suppressing the page. So the model may **raise** severity
  and may not **lower** it. Escalation is a judgment call; de-escalation is an attack surface.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents.prompts.library import TRIAGE_SEVERITY
from agents.topology import dependents_of
from agents.triage.classifier import CRITICAL_SERVICES, IMPORTANT_SERVICES, classify_severity
from core.logging import get_logger
from db.enums import Severity
from guardrails import guard_input, guard_output
from providers.base import LLMProvider

_log = get_logger(component="agent.triage")

# Ordered worst → best so severities can be compared without parsing the digit out.
_RANK = {Severity.P1: 0, Severity.P2: 1, Severity.P3: 2, Severity.P4: 3}


class TriageJudgment(BaseModel):
    """The model's severity assessment."""

    severity: Literal["P1", "P2", "P3", "P4"] = Field(
        description="Severity that best matches customer impact."
    )
    customer_impact: str = Field(
        description="One sentence on what customers actually experience.", max_length=400
    )
    reasoning: str = Field(
        description="Why this severity, referring to the alert content.", max_length=800
    )


class TriageOutcome(BaseModel):
    """What Triage hands to the rest of the graph."""

    severity: Severity
    floor_severity: Severity
    model_severity: Severity | None
    escalated: bool
    clamped: bool
    customer_impact: str
    reasoning: str
    tokens_used: int = 0
    cost_usd: float = 0.0
    prompt_ref: str | None = None
    degraded: bool = False


def _service_role(service: str) -> str:
    if service in CRITICAL_SERVICES:
        return "revenue path — customers cannot complete a purchase without it"
    if service in IMPORTANT_SERVICES:
        return "customer-facing but degradable — browsing works in a reduced form"
    return "unknown role in the platform"


async def assess(
    provider: LLMProvider,
    *,
    service: str,
    kind: str,
    title: str,
    value: float | None = None,
) -> TriageOutcome:
    """Judge severity with a model, clamped so it can only escalate.

    Degrades to the floor rather than failing the incident: triage is on the critical path,
    and an incident that stops at triage because a free-tier model was throttled is worse
    than one that proceeds with the rule-based severity.
    """
    floor = classify_severity(service, kind)
    prompt = TRIAGE_SEVERITY.render(
        title=title,
        service=service,
        kind=kind,
        value="not reported" if value is None else value,
        service_role=_service_role(service),
        dependents=", ".join(dependents_of(service)) or "none known",
        floor_severity=floor,
    )
    # Alert titles come from Alertmanager labels, which are attacker-reachable in the same
    # way logs are, so the rendered prompt is screened rather than trusted.
    guarded = guard_input(prompt, agent="triage")

    try:
        structured = await provider.complete_structured(
            guarded.prompt,
            schema=TriageJudgment,
            agent="triage",
            system=TRIAGE_SEVERITY.system,
            prompt_ref=TRIAGE_SEVERITY.ref,
        )
    except Exception as exc:  # noqa: BLE001 — never let triage block the investigation
        _log.warning("triage_llm_unavailable", service=service, error=str(exc))
        return TriageOutcome(
            severity=floor,
            floor_severity=floor,
            model_severity=None,
            escalated=False,
            clamped=False,
            customer_impact="not assessed",
            reasoning=(
                f"severity from the rule-based floor; model unavailable ({type(exc).__name__})"
            ),
            degraded=True,
        )

    judgment = structured.value
    guard_output(judgment.reasoning, agent="triage")
    model_severity = Severity(judgment.severity)

    # The clamp. A model may escalate but never de-escalate — see the module docstring.
    clamped = _RANK[model_severity] > _RANK[floor]
    final = floor if clamped else model_severity
    if clamped:
        _log.info(
            "triage_severity_clamped",
            service=service,
            floor=str(floor),
            model_proposed=str(model_severity),
        )

    return TriageOutcome(
        severity=final,
        floor_severity=floor,
        model_severity=model_severity,
        escalated=_RANK[final] < _RANK[floor],
        clamped=clamped,
        customer_impact=judgment.customer_impact,
        reasoning=judgment.reasoning,
        tokens_used=structured.result.tokens_used,
        cost_usd=structured.result.cost_usd,
        prompt_ref=structured.result.prompt_ref,
    )
