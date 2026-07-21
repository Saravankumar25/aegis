"""Agent explainability: a structured account of what an agent did and why (ESD §5, §9).

An on-call engineer deciding whether to trust an automated conclusion currently has two
options: read the agent's one-line summary, which is too thin, or read the raw prompts,
evidence blocks and structured output, which is too much at 3am. This module produces the
thing in between — a fixed set of fields the frontend can render identically for every
agent, generated as structured output so it renders consistently rather than as prose that
happens to be formatted differently each run.

Three properties this is built around:

* **Never load-bearing.** Explanation happens after the agent's real work and cannot change
  it. A failed or unavailable explanation is recorded as absent; it never fails a step,
  never retries into the investigation's token budget more than once, and never blocks the
  graph. An investigation that cannot explain itself is worse than one that can, but an
  investigation that *stalls* because it could not explain itself is worse still.
* **Derived, not re-reasoned.** The explainer is given what the agent received and what it
  concluded, and asked to describe that. It is explicitly not asked to re-decide, because a
  second model rendering its own opinion as "what the agent did" is a fabrication with the
  authority of an audit record.
* **Uncertainty is a required field.** Making it structurally required is what stops
  explanations from reading as uniformly confident. An empty `uncertainty` is a claim, and
  the prompt says so.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agents.accounting import LlmAccounting
from agents.prompts.library import AGENT_EXPLANATION
from core.logging import get_logger
from guardrails import guard_input
from providers.base import LLMProvider

_log = get_logger(component="agent.explain")

# Explanations are a fixed, small cost per step. Capped tightly because they are a reading
# aid, not analysis: a long explanation is a failed explanation.
EXPLANATION_MAX_TOKENS = 700


class AgentExplanation(BaseModel):
    """One agent's execution, rendered for a human.

    Field names are part of the frontend contract — the UI renders these directly, so
    renaming one is a breaking change to the incident view, not an internal refactor.
    """

    headline: str = Field(
        description="One sentence an engineer could read alone and still be correctly informed.",
        max_length=200,
    )
    what_it_received: str = Field(description="The inputs this agent worked from.", max_length=600)
    evidence_collected: list[str] = Field(
        default_factory=list,
        description="Distinct pieces of evidence gathered, each one short.",
    )
    tools_used: list[str] = Field(default_factory=list, description="MCP tools invoked, by name.")
    documents_retrieved: list[str] = Field(
        default_factory=list,
        description="Runbook passages retrieved, and why each was relevant.",
    )
    reasoning: str = Field(
        description="How it got from the evidence to its conclusion.", max_length=1200
    )
    alternatives_considered: list[str] = Field(
        default_factory=list,
        description="Other explanations weighed, each with why it was set aside.",
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in the conclusion, calibrated to the evidence."
    )
    uncertainty: str = Field(
        description=(
            "What remains unproven or assumed. Required — an empty value asserts there is "
            "no uncertainty."
        ),
        max_length=600,
    )
    recommended_next: list[str] = Field(
        default_factory=list, description="What a human should do or check next."
    )


class ExplanationOutcome(LlmAccounting):
    """The explanation plus its accounting, or a recorded absence."""

    explanation: AgentExplanation | None = None
    degraded: bool = False
    reason: str = ""


def _bullets(items: list[str] | None, empty: str = "(none)") -> str:
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


async def explain_step(
    provider: LLMProvider | None,
    *,
    agent: str,
    title: str,
    service: str,
    inputs: str,
    evidence: list[str] | None = None,
    tools_used: list[str] | None = None,
    retrieved_docs: list[str] | None = None,
    output: str,
) -> ExplanationOutcome:
    """Describe one agent execution. Never raises; never blocks the caller."""
    if provider is None:
        return ExplanationOutcome(degraded=True, reason="no provider configured")

    prompt = AGENT_EXPLANATION.render(
        agent=agent,
        title=title,
        service=service,
        inputs=inputs or "(nothing recorded)",
        evidence=_bullets(evidence, "(no evidence collected)"),
        tools_used=", ".join(tools_used or []) or "(none)",
        retrieved_docs=_bullets(retrieved_docs, "(none retrieved)"),
        output=output or "(no conclusion recorded)",
    )
    # The evidence and output blocks contain attacker-reachable text from logs and commit
    # messages, so the explainer is screened exactly like every other call site.
    guarded = guard_input(prompt, agent="explain")

    try:
        structured = await provider.complete_structured(
            guarded.prompt,
            schema=AgentExplanation,
            agent="explain",
            system=AGENT_EXPLANATION.system,
            prompt_ref=AGENT_EXPLANATION.ref,
            max_tokens=EXPLANATION_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 — explaining must never break the thing explained
        _log.warning("explanation_unavailable", agent=agent, error=str(exc))
        return ExplanationOutcome(
            degraded=True, reason=f"explanation unavailable ({type(exc).__name__})"
        )

    return ExplanationOutcome(
        explanation=structured.value,
        **LlmAccounting.from_result(structured.result),
    )
