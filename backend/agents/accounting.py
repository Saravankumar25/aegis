"""Per-call LLM accounting carried on every agent outcome (ESD §15, §20).

Each agent already reported `tokens_used`/`cost_usd`/`prompt_ref` by declaring them
itself, which meant adding a field meant editing five models and updating five call
sites — and the two fields that mattered most for diagnosis were simply missing.

`model_used` in particular was being recorded as the *provider* name ("gemini") rather
than the model that actually answered ("gemini-3.1-flash-lite"). That silently defeats
the reason the provider owns tracing at all: it is the only layer that knows which model
answered after key rotation and model fallback, and that is exactly the field needed to
explain why one incident reasoned differently from another. An operator comparing two
incidents saw the same value on both.

`latency_ms` was never persisted at all, so no per-agent timing existed in Postgres
despite the column being defined in the first migration.
"""

from __future__ import annotations

from pydantic import BaseModel

from providers.base import LLMResult


class LlmAccounting(BaseModel):
    """Mixin for agent outcomes describing the LLM call(s) that produced them.

    Defaults describe the *degraded* case — no model answered — so an outcome produced
    by a deterministic fallback path is honestly reported as having used no model rather
    than inheriting a stale one.
    """

    model_used: str | None = None
    latency_ms: int | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0
    prompt_ref: str | None = None

    @staticmethod
    def from_result(result: LLMResult) -> dict[str, object]:
        """Accounting fields for a single call, spreadable into an outcome constructor.

        Returns a dict rather than an instance so it composes with models that add their
        own required fields: `Outcome(**LlmAccounting.from_result(r), severity=...)`.
        """
        return {
            "model_used": result.model,
            "latency_ms": result.latency_ms,
            "tokens_used": result.tokens_used,
            "cost_usd": result.cost_usd,
            "prompt_ref": result.prompt_ref,
        }

    @staticmethod
    def from_results(results: list[LLMResult]) -> dict[str, object]:
        """Accounting for an ensemble: summed cost, summed tokens, wall-clock latency.

        Latency is the max rather than the sum because ensemble passes are the thing a
        human waits on as a group; summing would report a number no one experienced.
        Models are joined when passes genuinely used different ones, which happens under
        fallback and is worth seeing rather than hiding behind the first pass's value.
        """
        if not results:
            return {}
        models = sorted({r.model for r in results})
        return {
            "model_used": ",".join(models),
            "latency_ms": max(r.latency_ms for r in results),
            "tokens_used": sum(r.tokens_used for r in results),
            "cost_usd": sum(r.cost_usd for r in results),
            "prompt_ref": results[0].prompt_ref,
        }
