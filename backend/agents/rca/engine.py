"""RCA Agent core: ensemble reasoning over delimited evidence (FR-3.1..FR-3.3).

Runs N ensemble passes through the configured provider, parses each into an ``RCAPass``
(one retry with a stricter prompt on malformed output, then that pass is dropped —
ESD §12 degradation, never a crashed incident), computes the agreement score, and returns
the consensus hypothesis. Runbook context (FR-3.3) is included as *titles + snippets*,
clearly separated from live evidence so citations can only point at real evidence ids.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationError

from agents.evidence import EvidenceStore
from agents.prompts.library import RCA_HYPOTHESIS
from agents.rca.scoring import (
    UNKNOWN_CATEGORY,
    RCAPass,
    agreement_score,
    consensus_pass,
)
from core.config import get_settings
from db.enums import EvidenceType
from guardrails import guard_input
from providers.base import LLMProvider, LLMResult
from providers.parsing import parse_json_object
from redaction.pipeline import EVIDENCE_RULES


class RCAResult(BaseModel):
    """Consensus output surfaced to the Observer and then to humans."""

    hypothesis: str
    root_cause_category: str
    confidence: float
    agreement_score: float
    low_confidence: bool
    claims: list[dict] = Field(default_factory=list)
    passes: list[RCAPass] = Field(default_factory=list)
    passes_requested: int = 0
    passes_succeeded: int = 0
    ensemble_degraded: bool = False
    models_used: list[str] = Field(default_factory=list)
    latency_ms: int | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    budget_degraded: bool = False

    @property
    def cause_identified(self) -> bool:
        """Whether a cause was actually named, as opposed to explicitly not determined.

        `confidence` describes certainty in whatever conclusion was reached, and "unknown"
        is a conclusion. That makes `confidence` alone unsafe to gate on: `unknown` at 0.95
        means "confident this evidence identifies nothing", which reads identically to a
        confident diagnosis if you only look at the number. Anything that decides to *act*
        must require `cause_identified` as well.
        """
        return self.root_cause_category != UNKNOWN_CATEGORY


_SCHEMA_HINT = (
    '{"root_cause_category": "<deploy_regression|resource_exhaustion|error_spike|'
    'latency_degradation|unknown>", "hypothesis": "<one sentence>", '
    '"confidence": <0..1>, "claims": [{"claim": "<sentence>", "evidence_id": "<E#>"}]}'
)


def build_prompt(
    service: str,
    title: str,
    store: EvidenceStore,
    runbook_context: str,
    correlation_summary: str = "",
) -> str:
    """Render the versioned RCA prompt (``rca.hypothesis``).

    Was an inline f-string. Moved onto the registry so an eval score can be attributed to the
    prompt version that produced it, and so a careless edit changes a fingerprint rather than
    silently changing behaviour.
    """
    gaps = "\n".join(f"- {g}" for g in store.gaps) or "- none"
    # A category whose only possible evidence source was unavailable cannot be
    # asserted. Spelling this out in the prompt stops the model reaching for the
    # most narratively satisfying cause (observed live: it blamed "a recent
    # deployment" while the deploy source was down and no deploy evidence existed).
    has_change_evidence = any(i.type == EvidenceType.diff for i in store.items)
    unassertable = (
        ""
        if has_change_evidence
        else (
            "\nIMPORTANT: no deploy/commit evidence was gathered, so you may NOT claim a "
            "deploy or code change caused this. If the evidence does not identify a cause, "
            'answer "unknown" — that is a correct and useful answer, not a failure.\n'
        )
    )
    return (
        RCA_HYPOTHESIS.render(
            evidence_rules=EVIDENCE_RULES,
            title=title,
            service=service,
            correlation_summary=correlation_summary or "(no correlation summary available)",
            gaps=gaps,
            unassertable=unassertable,
            evidence_block=store.prompt_block(),
            runbook_context=runbook_context or "(none found)",
        )
        # The schema hint stays outside the template: it is derived from `RCAPass`, so
        # keeping it here means the prompt cannot drift from the model it must satisfy.
        + f"\n\nRespond with JSON only, exactly this schema: {_SCHEMA_HINT}"
    )


# Confidence is not cosmetic: `consensus_pass` picks the published hypothesis with
# `max(..., key=confidence)`, so whichever pass reports the highest number is the one a human
# reads and acts on. A malformed value that resolves *upward* therefore wins the ensemble.
NEUTRAL_CONFIDENCE = 0.5


def _coerce_confidence(value: object) -> float:
    """Normalise a model's confidence into [0, 1], failing toward neutral, never toward 1.0.

    Three real coercion defects this replaces:

    * ``true`` scored **1.0**. ``isinstance(True, int)`` is True in Python and ``True > 1`` is
      False, so the percentage branch was skipped and Pydantic coerced the bool straight to
      maximum confidence — which then won ``consensus_pass`` outright. A model that cannot
      express a number is the *last* one whose pass should be published.
    * ``150`` became ``1.5`` and failed ``le=1.0``, dropping an otherwise valid pass. Losing a
      whole ensemble pass to a formatting quirk is exactly what this parser exists to prevent,
      and dropped passes then flip ``ensemble_degraded``.
    * a dict or list passed through untouched and dropped the pass at validation.

    Anything uninterpretable becomes ``NEUTRAL_CONFIDENCE`` rather than an extreme, because a
    parse failure is an absence of a signal, not evidence of certainty in either direction.
    """
    # bool first: it is an int subclass, so every numeric check below would accept it.
    if isinstance(value, bool):
        return NEUTRAL_CONFIDENCE
    if isinstance(value, str):
        try:
            value = float(value.strip().rstrip("%"))
        except ValueError:
            return NEUTRAL_CONFIDENCE
    if not isinstance(value, int | float):
        return NEUTRAL_CONFIDENCE
    number = float(value)
    if number != number:  # NaN — comparisons below would silently pass it through
        return NEUTRAL_CONFIDENCE
    if number > 1.0:
        number = number / 100.0  # a percentage, e.g. 85 -> 0.85
    return min(1.0, max(0.0, number))  # 150% -> 1.0, negatives -> 0.0, never a dropped pass


def _parse_pass(raw: str) -> RCAPass | None:
    """Parse one pass's JSON, tolerating the formatting real models actually emit.

    ``parse_json_object`` strips markdown fences and digs the object out of
    surrounding prose — both of which happen even with JSON mode enabled. A pass
    that still doesn't validate returns None and the caller runs its retry path.
    """
    payload = parse_json_object(raw)
    if payload is None:
        return None
    payload["confidence"] = _coerce_confidence(payload.get("confidence"))
    # Drop malformed claim entries rather than failing the whole pass.
    claims = payload.get("claims")
    payload["claims"] = (
        [c for c in claims if isinstance(c, dict)] if isinstance(claims, list) else []
    )
    try:
        return RCAPass.model_validate(payload)
    except ValidationError:
        return None


async def run_rca(
    provider: LLMProvider,
    *,
    service: str,
    title: str,
    store: EvidenceStore,
    runbook_context: str = "",
    correlation_summary: str = "",
    tokens_already_used: int = 0,
) -> RCAResult:
    """Ensemble RCA with per-incident budget degradation (ESD §15)."""
    settings = get_settings()
    prompt = build_prompt(service, title, store, runbook_context, correlation_summary)
    # Evidence text is attacker-reachable; the rendered prompt is screened like any other.
    prompt = guard_input(prompt, agent="rca").prompt

    passes: list[RCAPass] = []
    results: list[LLMResult] = []
    tokens_used = tokens_already_used
    budget_degraded = False

    for i in range(settings.rca_ensemble_passes):
        if tokens_used >= settings.incident_token_budget:
            budget_degraded = True  # fewer passes rather than overspend or fail (ESD §15)
            break
        result = await provider.complete(
            prompt,
            agent="rca",
            system=RCA_HYPOTHESIS.system,
            ensemble_pass=i,
            prompt_ref=RCA_HYPOTHESIS.ref,
        )
        results.append(result)
        tokens_used += result.tokens_used
        parsed = _parse_pass(result.text)
        if parsed is None:
            # One stricter retry (ESD §12), then drop the pass.
            strict = await provider.complete(
                prompt + "\n\nSTRICT: your previous output was not valid JSON. "
                "Return ONLY the JSON object.",
                agent="rca",
                ensemble_pass=i,
            )
            results.append(strict)
            tokens_used += strict.tokens_used
            parsed = _parse_pass(strict.text)
        if parsed is not None:
            passes.append(parsed)

    models_used = sorted({r.model for r in results})

    if not passes:
        return RCAResult(
            hypothesis="RCA could not produce a valid hypothesis from the available evidence.",
            root_cause_category="unknown",
            confidence=0.0,
            agreement_score=0.0,
            low_confidence=True,
            passes_requested=settings.rca_ensemble_passes,
            passes_succeeded=0,
            ensemble_degraded=True,
            models_used=models_used,
            latency_ms=max((r.latency_ms for r in results), default=None),
            tokens_used=sum(r.tokens_used for r in results),
            cost_usd=sum(r.cost_usd for r in results),
            budget_degraded=budget_degraded,
        )

    score = agreement_score(passes)
    best = consensus_pass(passes)
    # A single surviving pass "agrees with itself" — that is arithmetic, not
    # corroboration. Reporting 1.00 there would present one opinion as unanimity,
    # so a degraded ensemble is always flagged low-confidence regardless of score
    # (PRD 10A: surface disagreement, never manufacture certainty).
    ensemble_degraded = len(passes) < min(2, settings.rca_ensemble_passes)
    low_confidence = score < settings.rca_agreement_threshold or ensemble_degraded
    return RCAResult(
        hypothesis=best.hypothesis,
        root_cause_category=best.root_cause_category,
        confidence=best.confidence,
        agreement_score=score,
        low_confidence=low_confidence,
        claims=best.claims,
        passes=passes,
        passes_requested=settings.rca_ensemble_passes,
        passes_succeeded=len(passes),
        ensemble_degraded=ensemble_degraded,
        models_used=models_used,
        latency_ms=max((r.latency_ms for r in results), default=None),
        tokens_used=sum(r.tokens_used for r in results),
        cost_usd=sum(r.cost_usd for r in results),
        budget_degraded=budget_degraded,
    )
