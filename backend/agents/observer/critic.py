"""Observer semantic critique: the LLM half of validation (FR-8.1, ESD §16).

This runs **alongside** `validator.review`, never instead of it. The split follows what each
mechanism is actually good at:

* `validator.py` (deterministic) answers *mechanical* questions with certainty — does this
  citation resolve to real evidence, does the cited text contain a signal consistent with the
  asserted category, does any evidence look like injected instructions. It cannot be argued
  out of its own rules, which is exactly why it holds the veto.
* This module answers the *semantic* question the regexes cannot — does the cited passage
  actually **mean** what the claim says it means. `restarts=0` matches a "restart" marker
  while being evidence of health; only a reader can tell.

**Both must pass.** A hypothesis is approved only if the deterministic checks pass AND the
critic does not reject. This is deliberately asymmetric: the critic can *veto* an otherwise
clean hypothesis, but it can never *rescue* one the deterministic layer rejected. An LLM
persuaded by injected text must not be able to approve what the machinery refused — that would
hand the attacker the very veto the deterministic layer exists to keep.

The critic is prompted adversarially (find why this is wrong) and defaults to rejection on
uncertainty, because the costs are asymmetric: a wrong hypothesis acted on during an outage
does real damage, while a rejected correct one costs one more pass.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents.evidence import EvidenceStore
from agents.prompts.library import OBSERVER_CRITIQUE
from core.logging import get_logger
from guardrails import guard_input
from providers.base import LLMProvider
from redaction.pipeline import EVIDENCE_RULES

_log = get_logger(component="agent.observer.critic")


class CritiqueResult(BaseModel):
    """The critic's adversarial assessment."""

    # Literal, not a described `str`: the allowed values belong in the schema so the API's
    # structured-output mode and Pydantic validation both enforce them. Stating them only in
    # prose meant a model could return "approve with reservations" and be read as a rejection.
    verdict: Literal["approve", "reject"] = Field(
        description="'approve' if the hypothesis survives scrutiny, otherwise 'reject'."
    )
    reason: str = Field(description="The single strongest reason for the verdict.", max_length=800)
    unsupported_claims: list[str] = Field(
        default_factory=list,
        description="Claims whose cited evidence does not actually establish them.",
    )
    alternative_cause: str | None = Field(
        default=None,
        description="A different cause at least as consistent with the same evidence, if any.",
    )

    @property
    def approved(self) -> bool:
        # Anything that is not an explicit approval counts as a rejection. A malformed or
        # evasive verdict must not read as consent.
        return self.verdict.strip().lower() == "approve"


class CritiqueOutcome(BaseModel):
    """What the critic contributes to the Observer verdict."""

    ran: bool
    approved: bool
    reason: str
    unsupported_claims: list[str] = Field(default_factory=list)
    alternative_cause: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    prompt_ref: str | None = None


def _claims_block(claims: list[dict], store: EvidenceStore) -> str:
    """Render each claim next to the evidence it cites, so the critic can compare them."""
    lines = []
    for index, claim in enumerate(claims, start=1):
        text = str(claim.get("claim", "")).strip()
        evidence_id = str(claim.get("evidence_id", "") or "")
        item = store.get(evidence_id) if evidence_id else None
        cited = item.summary if item is not None else "(no such evidence)"
        lines.append(f"{index}. CLAIM: {text}\n   CITES {evidence_id or '(nothing)'}: {cited}")
    return "\n".join(lines) or "(no claims presented)"


async def critique(
    provider: LLMProvider | None,
    *,
    hypothesis: str,
    category: str,
    confidence: float,
    claims: list[dict],
    store: EvidenceStore,
) -> CritiqueOutcome:
    """Adversarially review a hypothesis.

    Degrades to ``ran=False`` when the model is unavailable. That is safe **only** because
    the deterministic validator still holds the veto: losing the critic weakens the review to
    what it was before, it does not approve anything the machinery would have caught.
    """
    if provider is None:
        return CritiqueOutcome(ran=False, approved=True, reason="critic not configured")

    prompt = OBSERVER_CRITIQUE.render(
        evidence_rules=EVIDENCE_RULES,
        hypothesis=hypothesis,
        category=category,
        confidence=confidence,
        claims_block=_claims_block(claims, store),
        gaps="\n".join(f"- {g}" for g in store.gaps) or "- none",
    )
    # The claims block embeds evidence text, which is attacker-reachable.
    guarded = guard_input(prompt, agent="observer")

    try:
        structured = await provider.complete_structured(
            guarded.prompt,
            schema=CritiqueResult,
            agent="observer",
            system=OBSERVER_CRITIQUE.system,
            prompt_ref=OBSERVER_CRITIQUE.ref,
        )
    except Exception as exc:  # noqa: BLE001 — deterministic validation still stands
        _log.warning("observer_critic_unavailable", error=str(exc))
        return CritiqueOutcome(
            ran=False,
            approved=True,
            reason=f"semantic critique unavailable ({type(exc).__name__}); "
            f"deterministic validation still applied",
        )

    result = structured.value
    if not result.approved:
        _log.info("observer_critic_rejected", category=category, reason=result.reason[:200])
    return CritiqueOutcome(
        ran=True,
        approved=result.approved,
        reason=result.reason,
        unsupported_claims=result.unsupported_claims,
        alternative_cause=result.alternative_cause,
        tokens_used=structured.result.tokens_used,
        cost_usd=structured.result.cost_usd,
        prompt_ref=structured.result.prompt_ref,
    )
