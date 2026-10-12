"""Approval decisions and their runnable work commit as one unit."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aegis.api.routers.approvals import DecisionRequest, decide
from aegis.api.security import Principal


class FailingEnqueueConnection:
    def __init__(self) -> None:
        self.pending: list[str] = []

    async def fetchrow(self, query: str, *args):
        if query.lstrip().startswith("UPDATE approvals"):
            self.pending.append("decision")
            return {
                "id": args[0],
                "action_id": "action-1",
                "incident_id": "incident-1",
                "decision": args[1],
            }
        if query.lstrip().startswith("INSERT INTO workflow_jobs"):
            raise RuntimeError("simulated enqueue failure")
        raise AssertionError(f"unexpected query: {query}")

    async def execute(self, query: str, *args):
        if "INSERT INTO audit_log" in query:
            self.pending.append("audit")
        return "OK"


class FailingEnqueueDatabase:
    def __init__(self) -> None:
        self.conn = FailingEnqueueConnection()
        self.committed: list[str] = []
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        try:
            yield self.conn
        except RuntimeError:
            self.conn.pending.clear()
            self.rolled_back = True
            raise
        else:
            self.committed.extend(self.conn.pending)


@pytest.mark.asyncio
async def test_enqueue_failure_rolls_back_decision_and_audit() -> None:
    db = FailingEnqueueDatabase()
    approval = SimpleNamespace(id="approval-1", incident_id="incident-1")
    approvals = SimpleNamespace(
        get=lambda _: _approval(approval),
        current_time=lambda: datetime.now(UTC),
    )
    container = SimpleNamespace(
        approvals=approvals,
        incidents=SimpleNamespace(get=lambda _: _incident()),
        db=db,
        settings=SimpleNamespace(workflow_dispatch_backend="postgres"),
    )
    principal = Principal(
        uid="approver-1",
        email="approver@example.test",
        roles=frozenset({"approver"}),
        tenant_id="tenant-1",
        environments=frozenset({"staging"}),
    )

    with pytest.raises(RuntimeError, match="simulated enqueue failure"):
        await decide(
            "approval-1",
            DecisionRequest(decision="approved", note=""),
            principal,
            container,
        )

    assert db.rolled_back
    assert db.committed == []


async def _approval(approval):
    return approval


async def _incident():
    return SimpleNamespace(environment="staging")
