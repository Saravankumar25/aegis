"""Resolution Agent: LLM action selection under deterministic safety control (FR-4.1, ESD §16).

Replaces a `dict[category] -> action` lookup that could only ever recommend the one action
its category had been mapped to. That mapping could not weigh evidence, could not decline
when the evidence was thin, could not choose between two plausible actions, and — because
`resource_exhaustion` mapped to `scale_deployment` — proposed scaling for a memory leak just
as readily as for genuine saturation.

**What the model decides:** which catalogued action addresses this root cause (or that none
does), the parameters within a declared range, its confidence, and which alternatives it
rejected and why.

**What the model can never decide,** because these are the properties an injected log line
would attack:

* *whether an action exists.* Anything outside the catalog is refused, so no prompt can
  invent a tool.
* *its own tier.* Tier is read from the catalog by action_type after selection. A model that
  could name its tier could label a destructive action Tier-1 and route itself around human
  approval — the single most valuable thing an attacker could achieve here.
* *its blast radius, expiry, shadow status, or rate limit.* All computed outside.
* *parameter magnitude.* Bounded by the catalog's declared range, so "scale to 500 replicas"
  clamps to the maximum instead of executing.

This is the Triage clamping convention applied to a more dangerous surface (CLAUDE.md §2):
LLM judgment is used where judgment is the work, and clamped wherever safety depends on it.
The four execution gates in `engine.py` remain unchanged and unaware of this module — they
would refuse an unsafe action regardless of how it was chosen.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agents.accounting import LlmAccounting
from agents.prompts.library import RESOLUTION_PLAN
from agents.resolution.actions import CATALOG, ActionSpec
from agents.topology import dependents_of
from core.logging import get_logger
from guardrails import guard_input
from providers.base import LLMProvider
from redaction.pipeline import EVIDENCE_RULES

_log = get_logger(component="agent.resolution.planner")

# Declared bounds per tunable parameter. A relief step, not a capacity plan: the agent
# nudges a deployment while humans decide the real number, so a large jump is out of scope
# by construction rather than by the model's restraint.
PARAMETER_BOUNDS: dict[str, tuple[int, int]] = {
    "replicas_delta": (1, 3),
}

NO_ACTION = "none"


class ResolutionPlan(BaseModel):
    """The model's remediation decision."""

    action_type: str = Field(
        description=(
            "Exact action_type from the catalog, or 'none' when no catalogued action "
            "addresses this root cause."
        )
    )
    reasoning: str = Field(
        description="Why this action addresses this specific root cause.", max_length=1200
    )
    alternatives_rejected: list[str] = Field(
        default_factory=list,
        description="Other actions considered, each with the reason it was rejected.",
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence that this action addresses the root cause."
    )
    replicas_delta: int | None = Field(
        default=None,
        description="For scale_deployment only: how many replicas to add. Omit otherwise.",
    )
    expected_effect: str = Field(
        default="",
        description="What should observably change if this action works.",
        max_length=400,
    )


class ResolutionDecision(LlmAccounting):
    """What the planner hands back, after Python has constrained the model's choice."""

    spec: ActionSpec | None = None
    reasoning: str = ""
    alternatives_rejected: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    expected_effect: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)
    # True when the model named something outside the catalog. Surfaced rather than silently
    # treated as "no action": a model repeatedly reaching for a tool it does not have is a
    # signal about the catalog, and silently swallowing it hides that.
    invalid_selection: str | None = None
    clamped: list[str] = Field(default_factory=list)
    degraded: bool = False


def _render_catalog() -> str:
    """The catalog as the model sees it — deliberately without tier.

    Showing the tier invites the model to reason about approval requirements, which is not
    its decision and which biases selection toward whatever looks cheapest to authorise.
    """
    lines = []
    for spec in CATALOG.values():
        line = f"- {spec.action_type}: {spec.description} (targets a {spec.target_resource_type})"
        for param, (low, high) in PARAMETER_BOUNDS.items():
            if spec.action_type == "scale_deployment" and param == "replicas_delta":
                line += f"\n    parameter {param}: integer between {low} and {high}"
        lines.append(line)
    return "\n".join(lines)


def _clamp_parameters(plan: ResolutionPlan, spec: ActionSpec) -> tuple[dict[str, Any], list[str]]:
    """Build the execution parameters, clamping anything the model over-reached on."""
    parameters: dict[str, Any] = {}
    clamped: list[str] = []
    if spec.action_type == "scale_deployment":
        low, high = PARAMETER_BOUNDS["replicas_delta"]
        requested = plan.replicas_delta if plan.replicas_delta is not None else low
        bounded = max(low, min(high, int(requested)))
        if bounded != requested:
            clamped.append(f"replicas_delta {requested} -> {bounded}")
        parameters["replicas_delta"] = bounded
    return parameters, clamped


async def plan_remediation(
    provider: LLMProvider | None,
    *,
    root_cause_category: str,
    hypothesis: str,
    service: str,
    severity: str,
    target_pod: str | None,
    evidence_block: str,
) -> ResolutionDecision:
    """Choose a remediation, or decline.

    Degrades to *no action* when the model is unavailable — never to the old category
    mapping. A fallback that proposes an infrastructure change without the reasoning that
    justified it is exactly the "looks like AI, acts on a guess" behaviour this system is
    built to avoid; declining costs an operator one manual decision.
    """
    if provider is None:
        return ResolutionDecision(degraded=True, reasoning="no provider configured")

    prompt = RESOLUTION_PLAN.render(
        evidence_rules=EVIDENCE_RULES,
        root_cause_category=root_cause_category,
        hypothesis=hypothesis,
        service=service,
        severity=severity,
        dependents=", ".join(dependents_of(service)) or "nothing",
        target_pod=target_pod or "(none identified)",
        evidence_block=evidence_block or "(no evidence recorded)",
        action_catalog=_render_catalog(),
    )
    # The evidence block carries attacker-reachable log text.
    guarded = guard_input(prompt, agent="resolution")

    try:
        structured = await provider.complete_structured(
            guarded.prompt,
            schema=ResolutionPlan,
            agent="resolution",
            system=RESOLUTION_PLAN.system,
            prompt_ref=RESOLUTION_PLAN.ref,
        )
    except Exception as exc:  # noqa: BLE001 — an unavailable model must not act blindly
        _log.warning("resolution_planning_unavailable", error=str(exc))
        return ResolutionDecision(
            degraded=True,
            reasoning=(
                f"remediation planning unavailable ({type(exc).__name__}); no action proposed"
            ),
        )

    plan = structured.value
    accounting = LlmAccounting.from_result(structured.result)
    chosen = (plan.action_type or "").strip()

    if chosen.lower() in {NO_ACTION, "", "no_action"}:
        _log.info("resolution_declined", category=root_cause_category, reason=plan.reasoning[:200])
        return ResolutionDecision(
            reasoning=plan.reasoning,
            alternatives_rejected=plan.alternatives_rejected,
            confidence=plan.confidence,
            expected_effect=plan.expected_effect,
            **accounting,
        )

    spec = CATALOG.get(chosen)
    if spec is None:
        # Refused, not coerced to the nearest match: guessing what the model meant would
        # execute infrastructure changes on an inference.
        _log.warning(
            "resolution_selected_unknown_action",
            requested=chosen[:64],
            category=root_cause_category,
        )
        return ResolutionDecision(
            invalid_selection=chosen[:64],
            reasoning=plan.reasoning,
            alternatives_rejected=plan.alternatives_rejected,
            confidence=plan.confidence,
            **accounting,
        )

    parameters, clamped = _clamp_parameters(plan, spec)
    if clamped:
        _log.warning("resolution_parameters_clamped", action=spec.action_type, clamped=clamped)

    _log.info(
        "resolution_planned",
        action=spec.action_type,
        category=root_cause_category,
        confidence=plan.confidence,
        alternatives=len(plan.alternatives_rejected),
    )
    return ResolutionDecision(
        spec=spec,
        reasoning=plan.reasoning,
        alternatives_rejected=plan.alternatives_rejected,
        confidence=plan.confidence,
        expected_effect=plan.expected_effect,
        parameters=parameters,
        clamped=clamped,
        **accounting,
    )
