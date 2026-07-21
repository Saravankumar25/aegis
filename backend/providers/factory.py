"""Provider selection by configuration (Strategy pattern, ESD §20/§24).

There is exactly one provider: real models over OpenRouter. There is deliberately
no stub, mock, or offline provider in the application — a system that can silently
answer with fabricated reasoning is worse than one that fails loudly, because the
output is indistinguishable from a real analysis at the point where a human trusts
it. Tests use doubles from ``tests/support``, which are not importable as a runtime
provider.
"""

from __future__ import annotations

from core.config import get_settings
from providers.base import LLMProvider


def get_provider(name: str | None = None) -> LLMProvider:
    """Return the configured provider. Raises if it is not usable."""
    name = name or get_settings().llm_provider
    if name == "openrouter":
        from providers.openrouter import OpenRouterProvider

        return OpenRouterProvider()
    raise ValueError(
        f"unsupported LLM provider '{name}' — the only supported value is 'openrouter'. "
        "Set LLM_PROVIDER=openrouter and OPENROUTER_API_KEYS in .env."
    )
