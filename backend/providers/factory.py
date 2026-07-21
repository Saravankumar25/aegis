"""Provider selection by configuration (Strategy pattern, ESD §20/§24).

Every provider here calls a real model. There is deliberately no stub, mock, or
offline provider in the application — a system that can silently answer with
fabricated reasoning is worse than one that fails loudly, because the output is
indistinguishable from a real analysis at the point where a human trusts it. Tests
use doubles from ``tests/support``, which are not importable as a runtime provider.

Two vendors are supported so that one vendor's exhausted free tier cannot stop the
system reasoning altogether. They are alternatives, not a chain: falling back
*across* vendors automatically would make it unclear after the fact which model
produced a given hypothesis, and that attribution is what makes a quality regression
diagnosable. Switching is a deliberate config change.
"""

from __future__ import annotations

from core.config import get_settings
from providers.base import LLMProvider

_SUPPORTED = ("openrouter", "gemini")


def get_provider(name: str | None = None) -> LLMProvider:
    """Return the configured provider. Raises if it is not usable."""
    name = name or get_settings().llm_provider
    if name == "openrouter":
        from providers.openrouter import OpenRouterProvider

        return OpenRouterProvider()
    if name == "gemini":
        from providers.gemini import GeminiProvider

        return GeminiProvider()
    raise ValueError(
        f"unsupported LLM provider '{name}' — supported values are "
        f"{', '.join(_SUPPORTED)}. Set LLM_PROVIDER and the matching "
        "OPENROUTER_API_KEYS / GEMINI_API_KEYS in .env."
    )
