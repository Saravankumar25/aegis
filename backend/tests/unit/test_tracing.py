"""Unit tests: LangSmith tracing wiring (ESD §13).

Two properties, and the first one is why this file exists.

**Tracing must actually submit.** The original implementation constructed a
`langsmith.Client` and returned `enabled=True`, which looked correct and passed review,
but the SDK's `traceable`/`trace` helpers read the *process environment* rather than any
Client handed to them, and no-op unless `LANGSMITH_TRACING` is truthy. pydantic-settings
loads `.env` into a settings object without exporting it, so every span was silently
discarded — for two releases, while the log line said `langsmith_enabled`. The test
below asserts the environment bridge, because that is the part that was missing and
nothing else in the suite would notice its removal.

**Tracing must never be load-bearing.** Observability that can break a run is worse than
no observability, so the disabled path must be behaviourally identical.
"""

from __future__ import annotations

import os

import pytest

from core.config import get_settings
from core.tracing import reset_for_tests, trace_span, traced, tracing_enabled

_LANGSMITH_VARS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGSMITH_PROJECT",
)


@pytest.fixture(autouse=True)
def clean_tracing_state(monkeypatch):
    """Isolate global tracing state and the env vars it writes.

    `tracing_enabled()` mutates `os.environ` via `setdefault`, which monkeypatch does not
    track because it never saw the assignment. Without the explicit teardown below, a
    test that enables tracing leaves `LANGSMITH_TRACING=true` (and, worse, an endpoint
    pointing at a dead port) set for every later test in the session — which is exactly
    how this file first turned four unrelated provider tests red.
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


# --- the regression: enabling tracing must export what the SDK reads ----------------------


def test_enabling_tracing_exports_the_env_the_sdk_reads(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.setenv("LANGSMITH_PROJECT_UNUSED", "x")
    get_settings.cache_clear()
    reset_for_tests()

    assert tracing_enabled() is True
    # Without this, `traceable`/`trace` are no-ops and nothing is ever submitted.
    assert os.environ.get("LANGSMITH_TRACING") == "true"
    assert os.environ.get("LANGSMITH_API_KEY") == "lsv2_pt_test"
    assert os.environ.get("LANGSMITH_PROJECT") == get_settings().langsmith_project


def test_tracing_stays_disabled_without_an_api_key(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    get_settings.cache_clear()
    reset_for_tests()

    assert tracing_enabled() is False
    # Nothing is exported, so an unconfigured deployment cannot accidentally start
    # shipping traces to a default project.
    assert "LANGSMITH_TRACING" not in os.environ


# --- tracing is never load-bearing --------------------------------------------------------


def test_traced_returns_the_undecorated_function_when_disabled(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    get_settings.cache_clear()
    reset_for_tests()

    def target(x: int) -> int:
        return x * 2

    assert traced("noop")(target) is target, (
        "a disabled deployment must carry no wrapper at all, so traced and untraced "
        "behave identically"
    )


def test_span_is_transparent_when_disabled(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    get_settings.cache_clear()
    reset_for_tests()

    with trace_span("anything", incident_id="abc"):
        result = 1 + 1
    assert result == 2


def test_span_swallows_backend_failures(monkeypatch):
    """An unreachable LangSmith must not abort an investigation."""
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:1")  # nothing listening
    get_settings.cache_clear()
    reset_for_tests()

    executed = False
    with trace_span("unreachable", incident_id="abc"):
        executed = True
    assert executed, "the traced block must run even when the tracing backend is down"


class _Sentinel(Exception):
    """A caller's exception type, distinct from anything tracing raises."""


def test_exception_propagates_unchanged_through_an_enabled_span(monkeypatch):
    """Regression: an enabled span must not replace the caller's exception.

    `trace_span` is a @contextmanager. The naive `try: with trace(): yield / except:
    yield` shape yields a second time when the body raises, and contextlib converts that
    into `RuntimeError: generator didn't stop after throw()`. The original exception is
    destroyed — so `ProviderExhausted` stops matching `except ProviderExhausted` and the
    "fail loudly rather than fabricate" degradation path silently stops working, but only
    on deployments where tracing is switched on.
    """
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    get_settings.cache_clear()
    reset_for_tests()
    assert tracing_enabled() is True

    with pytest.raises(_Sentinel):
        with trace_span("failing", incident_id="abc"):
            raise _Sentinel("the caller's real failure")


def test_exception_propagates_when_the_tracing_backend_is_unreachable(monkeypatch):
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:1")
    get_settings.cache_clear()
    reset_for_tests()

    with pytest.raises(_Sentinel):
        with trace_span("failing", incident_id="abc"):
            raise _Sentinel("still the caller's failure")


def test_flush_is_safe_when_tracing_is_disabled(monkeypatch):
    from core.tracing import flush

    monkeypatch.setenv("LANGSMITH_API_KEY", "")
    get_settings.cache_clear()
    reset_for_tests()
    flush()  # must not raise
