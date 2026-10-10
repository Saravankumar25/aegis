"""Checkpointed Horizon state cannot raise supervisor-owned budget caps."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aegis.agents.horizon.orchestrator import HorizonOrchestrator
from aegis.core.clock import FrozenClock
from aegis.core.config import Settings
from aegis.core.errors import BudgetExhausted
from aegis.domain.horizon import HorizonState


def test_checkpoint_restores_usage_but_cannot_restore_larger_maxima() -> None:
    clock = FrozenClock()
    settings = Settings(
        agent_max_wall_seconds=60,
        agent_max_llm_calls=2,
        agent_max_tool_calls=3,
        agent_max_tokens=100,
        otel_traces_enabled=False,
    )
    orchestrator = HorizonOrchestrator.__new__(HorizonOrchestrator)
    orchestrator._s = settings
    orchestrator._d = SimpleNamespace(clock=clock)
    state = HorizonState(
        run_id="run_1",
        incident_id="inc_1",
        service="local:default:checkout",
        started_at=clock.now(),
        budget_usage={
            "llm_calls": 2,
            "tool_calls": 1,
            "tokens": 10,
            "max_llm_calls": 1_000_000,
            "max_tokens": 1_000_000_000,
        },
    )

    budget = orchestrator._budget(state)

    assert budget.max_llm_calls == 2
    assert budget.max_tokens == 100
    with pytest.raises(BudgetExhausted, match="llm calls"):
        budget.check("resume")


def test_budget_snapshot_contains_usage_not_authority() -> None:
    settings = Settings(otel_traces_enabled=False)
    orchestrator = HorizonOrchestrator.__new__(HorizonOrchestrator)
    orchestrator._s = settings
    orchestrator._d = SimpleNamespace(clock=FrozenClock())
    budget = orchestrator._budget(
        HorizonState(run_id="run_1", incident_id="inc_1", service="local:default:checkout")
    )

    assert set(budget.snapshot()) == {"llm_calls", "tool_calls", "tokens", "elapsed_s"}
