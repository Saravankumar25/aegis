"""Phase 1 guarantees: atomic enqueue and idempotent external dispatch."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from pydantic import ValidationError

from aegis.core.config import Settings
from aegis.persistence.dispatch_outbox import DispatchRecord
from aegis.persistence.jobs import JobQueue
from aegis.worker.dispatch import StepFunctionsDispatchAdapter, WorkflowDispatcher

pytestmark = pytest.mark.asyncio


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, str] | None:
        self.statements.append((query, args))
        return {"id": str(args[0])}

    async def execute(self, query: str, *args: Any) -> str:
        self.statements.append((query, args))
        return "INSERT 0 1"


class FakeDatabase:
    def __init__(self) -> None:
        self.conn = FakeConnection()
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self) -> Any:
        self.transactions += 1
        yield self.conn


async def test_enqueue_commits_job_and_dispatch_request_in_one_transaction() -> None:
    db = FakeDatabase()
    job_id = await JobQueue(  # type: ignore[arg-type]
        db, "step_functions"
    ).enqueue(incident_id="inc_1", payload={"trigger": "test"})

    assert job_id is not None
    assert db.transactions == 1
    sql = " ".join(statement for statement, _ in db.conn.statements)
    assert "INSERT INTO workflow_jobs" in sql
    # The migration trigger inserts the outbox row; enqueue selects the adapter
    # destination without opening a second transaction.
    assert "UPDATE workflow_dispatch_outbox" in sql
    outbox_args = next(args for query, args in db.conn.statements if "outbox" in query)
    assert outbox_args == (job_id, "step_functions")


class AlreadyExists(Exception):
    response = {"Error": {"Code": "ExecutionAlreadyExists"}}


class DuplicateStepFunctionsClient:
    def start_execution(self, **_: Any) -> None:
        raise AlreadyExists


async def test_step_functions_duplicate_is_the_same_successful_dispatch() -> None:
    settings = Settings(
        step_functions_state_machine_arn="arn:aws:states:us-east-1:123:stateMachine:aegis",
        workflow_dispatch_attempts=1,
        otel_traces_enabled=False,
    )
    adapter = StepFunctionsDispatchAdapter(settings, client=DuplicateStepFunctionsClient())
    record = DispatchRecord(
        id="dispatch_1",
        job_id="job_1",
        incident_id="inc_1",
        destination="step_functions",
        kind="investigate",
        payload={},
        attempts=2,
        max_attempts=5,
        lease_version=2,
    )

    assert await adapter.dispatch(record) == "step-functions:job_1"


async def test_step_functions_backend_requires_state_machine_arn() -> None:
    with pytest.raises(ValidationError, match="State Machine ARN"):
        Settings(
            workflow_dispatch_backend="step_functions",
            step_functions_state_machine_arn="",
            otel_traces_enabled=False,
        )


class FakeOutbox:
    def __init__(self, records: list[DispatchRecord]) -> None:
        self.records = records
        self.dispatched: list[tuple[str, str]] = []
        self.recovered = 0

    async def recover_stale(self) -> int:
        self.recovered += 1
        return 0

    async def claim(self, _: str, destination: str) -> DispatchRecord | None:
        if not self.records:
            return None
        record = self.records.pop(0)
        assert record.destination == destination
        return record

    async def mark_dispatched(self, record_id: str, external_ref: str, **_: Any) -> bool:
        self.dispatched.append((record_id, external_ref))
        return True

    async def mark_failed(self, *_: Any, **__: Any) -> None:
        raise AssertionError("successful dispatch must not be marked failed")


class CountingAdapter:
    destination = "postgres"

    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, record: DispatchRecord) -> str:
        self.calls += 1
        return f"postgres:{record.job_id}"


async def test_dispatcher_claims_each_outbox_record_once() -> None:
    record = DispatchRecord(
        id="dispatch_1",
        job_id="job_1",
        incident_id="inc_1",
        destination="postgres",
        kind="investigate",
        payload={},
        attempts=1,
        max_attempts=5,
        lease_version=1,
    )
    outbox = FakeOutbox([record])
    adapter = CountingAdapter()

    count = await WorkflowDispatcher(outbox, adapter).dispatch_batch(  # type: ignore[arg-type]
        "dispatcher-1", limit=10
    )

    assert count == 1
    assert adapter.calls == 1
    assert outbox.recovered == 1
    assert outbox.dispatched == [("dispatch_1", "postgres:job_1")]
