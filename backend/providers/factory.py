"""Provider selection by configuration (Strategy pattern, ESD §20/§24)."""

from __future__ import annotations

from core.config import get_settings
from providers.base import LLMProvider
from providers.stub import StubProvider


def get_provider(name: str | None = None) -> LLMProvider:
    """Return the configured provider. Default is the deterministic stub (no key, no cost).

    Real providers (Claude / Groq / Ollama, ESD §20) plug in here behind the same interface;
    they are deliberately not implemented until the eval harness exists to measure them
    (BUILD_LOG M6 decision), so nothing in the agent code changes when they land.
    """
    name = name or get_settings().llm_provider
    if name == "stub":
        return StubProvider()
    raise ValueError(
        f"unknown LLM provider '{name}' — only 'stub' ships in MVP; see providers/factory.py"
    )
