"""LLM guardrails: governing the model interaction itself (ESD §16).

Distinct from the Observer, and the distinction is load-bearing:

* The **Observer** judges *reasoning* — is this hypothesis supported by the evidence it cites?
  It runs once, at the end, on a finished analysis.
* **Guardrails** govern *every model interaction* — what goes into a prompt, and what is
  allowed back out. They run on every call, including calls whose output no Observer ever
  sees (a stakeholder update, a routing decision, a tool selection).

An Observer alone leaves the majority of LLM traffic ungoverned, which is why both exist.

Guardrails are **deterministic on purpose**. A model asked to detect prompt injection is
itself a prompt-injection target: the check and the thing being checked share a failure mode,
so a payload that fools one plausibly fools both. Pattern-based screening cannot be talked out
of its own rules. It is weaker at nuance and stronger at exactly the thing that matters here —
which is why the LLM Observer critique (`agents/observer/critic.py`) sits *alongside* it
rather than replacing it.
"""

from __future__ import annotations

from guardrails.policy import (
    GuardrailViolation,
    InputGuardResult,
    OutputGuardResult,
    guard_input,
    guard_output,
)

__all__ = [
    "GuardrailViolation",
    "InputGuardResult",
    "OutputGuardResult",
    "guard_input",
    "guard_output",
]
