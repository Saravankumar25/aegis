"""RCA Agent core: ensemble reasoning over delimited evidence (FR-3.1..FR-3.3).

Runs N ensemble passes through the configured provider, parses each into an ``RCAPass``
(one retry with a stricter prompt on malformed output, then that pass is dropped —
ESD §12 degradation, never a crashed incident), computes the agreement score, and returns
the consensus hypothesis. Runbook context (FR-3.3) is included as *titles + snippets*,
clearly separated from live evidence so citations can only point at real evidence ids.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from agents.evidence import EvidenceStore
from agents.rca.scoring import RCAPass, agreement_score, consensus_pass
from core.config import get_settings
from providers.base import LLMProvider, LLMResult
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
    tokens_used: int = 0
    cost_usd: float = 0.0
    budget_degraded: bool = False


_SCHEMA_HINT = (
    '{"root_cause_category": "<deploy_regression|resource_exhaustion|error_spike|'
    'latency_degradation|unknown>", "hypothesis": "<one sentence>", '
    '"confidence": <0..1>, "claims": [{"claim": "<sentence>", "evidence_id": "<E#>"}]}'
)


def build_prompt(service: str, title: str, store: EvidenceStore, runbook_context: str) -> str:
    gaps = "\n".join(f"- {g}" for g in store.gaps) or "- none"
    return (
        f"{EVIDENCE_RULES}\n\n"
        f"You are the RCA agent investigating: {title} (service: {service}).\n"
        f"Unavailable evidence sources (documented gaps):\n{gaps}\n\n"
        f"Evidence:\n{store.prompt_block()}\n\n"
        f"Relevant runbook excerpts (background knowledge, NOT citable evidence):\n"
        f"{runbook_context or '(none found)'}\n\n"
        f"Respond with JSON only, exactly this schema: {_SCHEMA_HINT}\n"
        f"Every claim MUST cite an evidence id that appears above."
    )


def _parse_pass(raw: str) -> RCAPass | None:
    try:
        return RCAPass.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError):
        return None


async def run_rca(
    provider: LLMProvider,
    *,
    service: str,
    title: str,
    store: EvidenceStore,
    runbook_context: str = "",
    tokens_already_used: int = 0,
) -> RCAResult:
    """Ensemble RCA with per-incident budget degradation (ESD §15)."""
    settings = get_settings()
    prompt = build_prompt(service, title, store, runbook_context)

    passes: list[RCAPass] = []
    results: list[LLMResult] = []
    tokens_used = tokens_already_used
    budget_degraded = False

    for i in range(settings.rca_ensemble_passes):
        if tokens_used >= settings.incident_token_budget:
            budget_degraded = True  # fewer passes rather than overspend or fail (ESD §15)
            break
        result = await provider.complete(prompt, agent="rca", ensemble_pass=i)
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

    if not passes:
        return RCAResult(
            hypothesis="RCA could not produce a valid hypothesis from the available evidence.",
            root_cause_category="unknown",
            confidence=0.0,
            agreement_score=0.0,
            low_confidence=True,
            tokens_used=sum(r.tokens_used for r in results),
            cost_usd=sum(r.cost_usd for r in results),
            budget_degraded=budget_degraded,
        )

    score = agreement_score(passes)
    best = consensus_pass(passes)
    return RCAResult(
        hypothesis=best.hypothesis,
        root_cause_category=best.root_cause_category,
        confidence=best.confidence,
        agreement_score=score,
        low_confidence=score < settings.rca_agreement_threshold,
        claims=best.claims,
        passes=passes,
        tokens_used=sum(r.tokens_used for r in results),
        cost_usd=sum(r.cost_usd for r in results),
        budget_degraded=budget_degraded,
    )
