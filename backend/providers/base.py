"""LLM provider Strategy interface (ESD §20, §24 Strategy pattern).

Agents depend on this contract only; the concrete provider is chosen by configuration
(``LLM_PROVIDER``), never hardcoded. Every call is accounted (tokens/cost/latency) so
FR-8.2 auditing and the ESD §15 per-incident budget have real numbers to work with.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, Field


class LLMResult(BaseModel):
    """One completed LLM call with its accounting."""

    text: str
    model: str
    tokens_used: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    latency_ms: int = Field(ge=0)


class LLMProvider(ABC):
    """Strategy interface. Implementations must be side-effect free beyond the call itself."""

    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        agent: str,
        ensemble_pass: int = 0,
        max_tokens: int = 1024,
    ) -> LLMResult:
        """Return a completion for ``prompt``. ``agent``/``ensemble_pass`` let providers
        vary sampling per pass (and let the deterministic stub vary its emphasis)."""
