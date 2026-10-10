"""The on-demand worker has externally enforced, finite lifecycle controls."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aegis.worker import main as worker_main


class EmptyQueue:
    def __init__(self, _: Any) -> None:
        self.claims = 0

    async def claim(self, *_: Any, **__: Any) -> None:
        self.claims += 1


def bare_worker(*, max_jobs: int | None, max_seconds: float | None) -> Any:
    worker = worker_main.Worker.__new__(worker_main.Worker)
    worker.worker_id = "bounded-test"
    worker.concurrency = 1
    worker.max_jobs = max_jobs
    worker.max_seconds = max_seconds
    worker.stop_when_idle = True
    worker._monotonic = lambda: 0.0
    worker._stopping = asyncio.Event()
    worker._inflight = set()
    worker._db = object()
    worker._deadline = max_seconds
    return worker


@pytest.mark.asyncio
async def test_bounded_worker_exits_immediately_when_there_is_no_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = EmptyQueue(object())
    monkeypatch.setattr(worker_main, "JobQueue", lambda _: queue)

    await bare_worker(max_jobs=5, max_seconds=None)._poll_loop()

    assert queue.claims == 1


@pytest.mark.asyncio
async def test_time_budget_can_end_before_the_first_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue = EmptyQueue(object())
    monkeypatch.setattr(worker_main, "JobQueue", lambda _: queue)
    worker = bare_worker(max_jobs=None, max_seconds=0.5)
    worker._monotonic = lambda: 1.0

    await worker._poll_loop()

    assert queue.claims == 0


def test_worker_cli_cannot_raise_agent_budgets() -> None:
    options = {
        option
        for action in worker_main.parser()._actions  # noqa: SLF001 - CLI contract test
        for option in action.option_strings
    }
    assert "--once" in options
    assert "--max-jobs" in options
    assert "--max-seconds" in options
    assert not any("budget" in option or "token" in option or "llm" in option for option in options)
