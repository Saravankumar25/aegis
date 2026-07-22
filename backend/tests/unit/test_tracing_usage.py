"""Unit tests: LLM token/cost accounting actually reaches LangSmith's rollup (ESD §13).

The regression this file exists for: every LLM span carried its accounting as plain
`extra.metadata` (`tokens_used=1234`, `cost_usd=0.004`), which LangSmith stores and
renders but never *sums*. Token rollups are computed only from
`extra.metadata["usage_metadata"]` — the SDK's `RunTree.set(usage_metadata=...)`
contract. So an investigation showed seventeen correctly nested spans and a trace total
of zero tokens: the per-incident cost budget (ESD §15) and the "why did this incident
cost 8x the last one" question had no data behind them, while the traces looked healthy.

The assertions below are deliberately about the *wire shape* rather than about a helper
being called, because the wire shape is the whole bug. `annotate()` and `record_usage()`
look interchangeable at the call site and are not.

Nothing here touches the network: the current run tree is stubbed, so a detached
`RunTree` collects what would have been submitted.
"""

from __future__ import annotations

import os

import httpx
import pytest

from core.config import get_settings
from core.tracing import annotate, record_usage, reset_for_tests, tracing_enabled
from providers.base import LLMResult

pytestmark = pytest.mark.anyio

_LANGSMITH_VARS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_PROJECT",
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def clean_tracing_state(monkeypatch):
    """Isolate global tracing state and the env vars `tracing_enabled()` exports.

    Same teardown as `test_tracing.py`: `setdefault` writes monkeypatch never saw, so
    without restoring them by hand an enabled test leaks `LANGSMITH_TRACING=true` into
    every later test in the session.
    """
    saved = {var: os.environ.get(var) for var in _LANGSMITH_VARS}
    for var in _LANGSMITH_VARS:
        monkeypatch.delenv(var, raising=False)
    reset_for_tests()
    get_settings.cache_clear()
    yield
    for var, value in saved.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value
    reset_for_tests()
    get_settings.cache_clear()


@pytest.fixture
def current_run(monkeypatch):
    """Enable tracing and stand a detached `RunTree` in for the current span.

    A real `RunTree` rather than a stub, so `set(usage_metadata=...)` runs the SDK's own
    key validation — a test double would happily accept keys LangSmith rejects, which is
    precisely the class of mistake this file is guarding.
    """
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    get_settings.cache_clear()
    reset_for_tests()
    assert tracing_enabled() is True

    from langsmith import run_helpers, run_trees

    run = run_trees.RunTree(name="llm:test", run_type="llm")
    monkeypatch.setattr(run_helpers, "get_current_run_tree", lambda: run)
    return run


def _usage(run) -> dict:
    return (run.extra.get("metadata") or {}).get("usage_metadata") or {}


# --- the regression: usage must land where LangSmith aggregates it -------------------------


def test_record_usage_writes_the_field_langsmith_aggregates(current_run):
    record_usage(
        model="fake/model-1",
        provider="openrouter",
        prompt_tokens=1200,
        completion_tokens=300,
        total_tokens=1500,
        cost_usd=0.0042,
    )

    assert _usage(current_run) == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "total_tokens": 1500,
        "total_cost": 0.0042,
    }, "these exact keys are what LangSmith sums; anything else is inert metadata"


def test_annotate_alone_does_not_produce_a_rollup(current_run):
    """The original bug, pinned so it cannot be reintroduced by 'simplifying' back.

    `annotate` is still the right tool for Aegis-specific diagnostics. It is the wrong
    tool for accounting, and the two are indistinguishable at the call site.
    """
    annotate(tokens_used=1500, cost_usd=0.0042)

    assert _usage(current_run) == {}
    assert current_run.extra["metadata"]["tokens_used"] == 1500


def test_model_and_provider_are_recorded_for_attribution(current_run):
    record_usage(model="google/gemini-x", provider="gemini", total_tokens=10)

    metadata = current_run.extra["metadata"]
    assert metadata["ls_model_name"] == "google/gemini-x"
    assert metadata["ls_provider"] == "gemini"


def test_an_unpriced_call_reports_no_cost_rather_than_zero(current_run):
    """Gemini returns no price. `total_cost=0.0` would assert the call was free.

    Omitting the key leaves it unknown, and lets LangSmith price the call itself from
    `ls_model_name` — claiming zero would suppress that and quietly understate spend.
    """
    record_usage(model="google/gemini-x", provider="gemini", total_tokens=900, cost_usd=0.0)

    usage = _usage(current_run)
    assert usage == {"total_tokens": 900}
    assert "total_cost" not in usage


def test_a_provider_reporting_only_a_total_still_rolls_up(current_run):
    record_usage(model="m", provider="p", total_tokens=77)

    assert _usage(current_run) == {"total_tokens": 77}


# --- accounting is observability, so it is never load-bearing ------------------------------


def test_record_usage_is_a_noop_when_tracing_is_disabled(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    get_settings.cache_clear()
    reset_for_tests()

    record_usage(model="m", provider="p", total_tokens=5)  # must not raise


def test_record_usage_swallows_a_hostile_run_tree(monkeypatch, current_run):
    """A backend or SDK change must degrade to missing numbers, never to a failed call."""

    class _Exploding:
        extra: dict = {}

        def set(self, **_: object) -> None:
            raise RuntimeError("SDK contract changed")

    from langsmith import run_helpers

    monkeypatch.setattr(run_helpers, "get_current_run_tree", lambda: _Exploding())
    record_usage(model="m", provider="p", total_tokens=5)  # must not raise


def test_flush_drains_the_client_the_spans_actually_use(monkeypatch):
    """Regression: `flush()` used to drain a queue nothing ever wrote to.

    Spans resolve their client through the SDK's module-level `get_cached_client()`,
    which is a different instance from the one `tracing_enabled()` constructs. Flushing
    only the latter emptied an already-empty queue and returned success, so a
    short-lived process — the worker finishing an incident — dropped whatever the atexit
    hook did not manage to submit, including the token accounting above.
    """
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    get_settings.cache_clear()
    reset_for_tests()
    assert tracing_enabled() is True

    from langsmith import run_trees

    flushed: list[str] = []

    class _Cached:
        def flush(self) -> None:
            flushed.append("cached")

    monkeypatch.setattr(run_trees, "get_cached_client", lambda **_: _Cached())

    from core.tracing import flush

    flush()
    assert "cached" in flushed, "the client the spans write to must be drained"


def test_flush_survives_a_failing_client(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    get_settings.cache_clear()
    reset_for_tests()
    assert tracing_enabled() is True

    from langsmith import run_trees

    class _Broken:
        def flush(self) -> None:
            raise RuntimeError("backend down")

    monkeypatch.setattr(run_trees, "get_cached_client", lambda **_: _Broken())

    from core.tracing import flush

    flush()  # must not raise


# --- wrap_llm_call feeds the real accounting through ---------------------------------------


async def test_wrap_llm_call_reports_the_split_and_the_answering_provider(current_run):
    from core.tracing import wrap_llm_call

    class _Provider:
        name = "openrouter"

        @wrap_llm_call
        async def complete(self, prompt: str, *, agent: str) -> LLMResult:
            return LLMResult(
                text="ok",
                model="fallback/model-2",
                tokens_used=1500,
                prompt_tokens=1200,
                completion_tokens=300,
                cost_usd=0.0042,
                latency_ms=456,
            )

    await _Provider().complete("hi", agent="rca")

    assert _usage(current_run) == {
        "input_tokens": 1200,
        "output_tokens": 300,
        "total_tokens": 1500,
        "total_cost": 0.0042,
    }
    # The model that *answered* after fallback, not the one originally requested — the
    # field that explains why one incident behaved differently from another.
    assert current_run.extra["metadata"]["ls_model_name"] == "fallback/model-2"
    assert current_run.extra["metadata"]["ls_provider"] == "openrouter"


# --- providers must supply the split the rollup depends on ---------------------------------


async def test_openrouter_parses_the_prompt_completion_split(monkeypatch):
    from providers.openrouter import OpenRouterProvider

    monkeypatch.setenv("OPENROUTER_API_KEYS", "key-alpha")
    monkeypatch.setenv("LLM_MODEL_DEFAULT", "model-primary")
    monkeypatch.setenv("LLM_MODEL_RCA", "model-primary")
    monkeypatch.setenv("LLM_MODEL_FALLBACKS", "model-primary")
    get_settings.cache_clear()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 300,
                    "total_tokens": 1500,
                    "cost": 0.0042,
                },
            },
        )

    provider = OpenRouterProvider(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await provider.complete("hi", agent="rca")

    assert (result.prompt_tokens, result.completion_tokens, result.tokens_used) == (1200, 300, 1500)
    get_settings.cache_clear()


async def test_gemini_counts_hidden_thinking_tokens_as_completion(monkeypatch):
    """`candidatesTokenCount` excludes reasoning tokens that were nonetheless billed.

    Reporting only the emitted text would make a thinking-heavy model look dramatically
    cheaper than it is, and the prompt/completion split would not add up to the total.
    """
    from providers.gemini import GeminiProvider

    monkeypatch.setenv("GEMINI_API_KEYS", "key-alpha")
    monkeypatch.setenv("GEMINI_MODEL_DEFAULT", "model-primary")
    monkeypatch.setenv("GEMINI_MODEL_RCA", "model-primary")
    monkeypatch.setenv("GEMINI_MODEL_FALLBACKS", "model-primary")
    get_settings.cache_clear()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 1200,
                    "candidatesTokenCount": 100,
                    "thoughtsTokenCount": 200,
                    "totalTokenCount": 1500,
                },
            },
        )

    provider = GeminiProvider(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = await provider.complete("hi", agent="rca")

    assert result.prompt_tokens == 1200
    assert result.completion_tokens == 300, "emitted text plus hidden reasoning"
    assert result.prompt_tokens + result.completion_tokens == result.tokens_used
    # Gemini reports no price; a fabricated rate card would be worse than a missing one.
    assert result.cost_usd == 0.0
    get_settings.cache_clear()
