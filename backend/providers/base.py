"""LLM provider Strategy interface (ESD §20, §24 Strategy pattern).

Agents depend on this contract only; the concrete provider is chosen by configuration
(``LLM_PROVIDER``), never hardcoded. Every call is accounted (tokens/cost/latency) so
FR-8.2 auditing and the ESD §15 per-incident budget have real numbers to work with.

Three call shapes, because agents genuinely need three different things:

* ``complete`` — free text. Used where the output is prose for a human.
* ``complete_structured`` — a validated Pydantic model. Used everywhere a downstream branch
  depends on the shape of the answer. Parsing free text into a decision is how a formatting
  artefact becomes a wrong routing decision, so agents that *act* on output use this.
* ``stream`` — tokens as they are produced, for surfaces where a human is waiting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import TypeVar

from pydantic import BaseModel, Field

StructuredT = TypeVar("StructuredT", bound=BaseModel)


class LLMResult(BaseModel):
    """One completed LLM call with its accounting."""

    text: str
    model: str
    tokens_used: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    latency_ms: int = Field(ge=0)
    # Which prompt produced this, as `id@version+fingerprint`. Recorded on the agent step so
    # a quality change can be attributed to a prompt edit rather than guessed at.
    prompt_ref: str | None = None
    # Repair attempts spent coercing the model into the required schema. Consistently
    # non-zero means the prompt or the schema needs work, not that the model is bad.
    repair_attempts: int = 0


class StructuredResult[StructuredT: BaseModel](BaseModel):
    """A validated structured output plus the accounting for the call that produced it."""

    model_config = {"arbitrary_types_allowed": True}

    value: StructuredT
    result: LLMResult


class LLMProvider(ABC):
    """Strategy interface. Implementations must be side-effect free beyond the call itself."""

    name: str = "base"

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        agent: str,
        system: str | None = None,
        ensemble_pass: int = 0,
        max_tokens: int = 1024,
        prompt_ref: str | None = None,
    ) -> LLMResult:
        """Return a completion for ``prompt``."""

    @abstractmethod
    async def complete_structured(
        self,
        prompt: str,
        *,
        schema: type[StructuredT],
        agent: str,
        system: str | None = None,
        ensemble_pass: int = 0,
        max_tokens: int = 1024,
        prompt_ref: str | None = None,
    ) -> StructuredResult[StructuredT]:
        """Return output validated against ``schema``.

        Implementations must *enforce* the schema — request it from the API where supported,
        validate the response, and repair a bounded number of times — then raise rather than
        return an unvalidated object. A caller branching on this result cannot distinguish a
        hallucinated field from a real one, so the guarantee has to live here.
        """

    @abstractmethod
    def stream(
        self,
        prompt: str,
        *,
        agent: str,
        system: str | None = None,
        max_tokens: int = 1024,
        prompt_ref: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas as the model produces them.

        Cancellation is cooperative: closing the iterator aborts the upstream request, so a
        caller that stops consuming stops paying for tokens.
        """
