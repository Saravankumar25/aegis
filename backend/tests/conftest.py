"""Suite-wide test isolation.

**Tests must never emit telemetry into the production observability project.** This was not
hypothetical: running the suite put 14 spans named `failing`, `unreachable` and `probe2-*`
into the real `aegis` LangSmith project — the project an on-call engineer would open to
diagnose a live incident. Individual tests had tried to point themselves at a dead endpoint,
but `RunTree` resolves its client through a process-wide singleton created at first use, so
a client built earlier against the real endpoint kept serving every later test.

The guarantee belongs here rather than in each test. A rule that every test author has to
remember is a rule that eventually gets forgotten, and the failure is silent — the suite
passes either way, and the pollution is only visible to whoever later wonders why production
traces are full of runs called `failing`.

Tests that genuinely need tracing enabled still monkeypatch these values and call
`core.tracing.reset_for_tests()`, which now also clears the SDK's client cache. They get a
fresh client pointed at whatever they set, and nothing reaches the real project.
"""

from __future__ import annotations

import os

import pytest

# Deliberately unreachable. Not a real project name that happens to be unused: a typo or a
# future default must fail to connect rather than quietly create a project and fill it.
_TEST_TRACING_ENV = {
    "LANGSMITH_TRACING": "false",
    "LANGSMITH_PROJECT": "aegis-test-suite",
    "LANGSMITH_ENDPOINT": "http://127.0.0.1:1",
    "LANGSMITH_API_KEY": "",
}


@pytest.fixture(scope="session", autouse=True)
def isolate_tracing_from_production() -> None:
    """Point every test's tracing at nothing, before any module-level client is built.

    Session-scoped and autouse so it lands before the first import that could construct a
    LangSmith client. Values are set unconditionally rather than with `setdefault`, because
    `setdefault` is precisely what let a real endpoint inherited from `.env` survive into the
    test process.
    """
    for key, value in _TEST_TRACING_ENV.items():
        os.environ[key] = value

    from core.config import get_settings
    from core.tracing import reset_for_tests

    get_settings.cache_clear()
    reset_for_tests()
