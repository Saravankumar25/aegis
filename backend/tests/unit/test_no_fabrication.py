"""The system must fail loudly rather than answer with fabricated reasoning.

Aegis ships no stub, mock, or offline LLM provider. When every model and key is
exhausted the call raises and the incident is left for the retry sweep. These tests
guard that property against a future "just add a fallback so it never fails" change,
which would reintroduce the exact failure mode this design exists to prevent: output
that looks identical to a real analysis at the moment a human decides to trust it.
"""

from __future__ import annotations

import httpx
import pytest

from agents.evidence import EvidenceStore
from agents.rca.engine import run_rca
from core.config import get_settings
from db.enums import EvidenceType
from providers.factory import get_provider
from providers.openrouter import OpenRouterProvider, ProviderExhausted
from tests.support.doubles import FailingLLM


def _store() -> EvidenceStore:
    store = EvidenceStore()
    store.add(
        type_=EvidenceType.log,
        source="k8s.get_pod_logs",
        ref="k8s/pod/x/log",
        text="OOMKilled by kernel",
    )
    return store


def test_no_stub_provider_is_importable():
    """The stub provider was deleted, not merely unregistered."""
    with pytest.raises(ModuleNotFoundError):
        __import__("providers.stub")


@pytest.mark.parametrize("name", ["stub", "mock", "fake", "offline", "dummy"])
def test_factory_refuses_every_non_real_provider(name: str):
    with pytest.raises(ValueError, match="only supported value is 'openrouter'"):
        get_provider(name)


async def test_exhausted_provider_raises_instead_of_fabricating():
    """No answer is produced when no model answered."""
    with pytest.raises(ProviderExhausted):
        await run_rca(FailingLLM(), service="payment-service", title="crashlooping", store=_store())


async def test_openrouter_raises_when_all_keys_and_models_are_throttled(monkeypatch):
    """Every model × key combination returns 429 — the provider must not invent output."""
    settings = get_settings()
    monkeypatch.setattr(settings, "openrouter_api_keys", "key-a,key-b")

    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(str(request.url))
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    provider = OpenRouterProvider(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderExhausted) as exc:
        await provider.complete("prompt", agent="rca")

    assert "fabricated" in str(exc.value)
    # Both keys tried against every model in the chain before giving up.
    assert len(attempts) >= 2 * len(provider._models_for("rca"))


async def test_openrouter_requires_keys(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "openrouter_api_keys", "")
    with pytest.raises(ValueError, match="OPENROUTER_API_KEYS is empty"):
        OpenRouterProvider()
