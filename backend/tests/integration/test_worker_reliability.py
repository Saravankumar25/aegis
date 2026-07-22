"""Integration tests for worker lifecycle, crash recovery and remediation atomicity.

Each test here corresponds to a defect that was reproduced against a real database before
it was fixed, not to a theorised risk. The reproductions are described in each docstring so
a later reader can tell what the test is defending and why the obvious simpler version of
the code is wrong.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest
from sqlalchemy import select

import worker.main as wm
from core.logging import configure_logging
from db.enums import (
    AlertSource,
    IncidentState,
    RemediationStatus,
    Severity,
)
from db.models import AuditLog, Incident, RemediationAction
from worker.main import Worker

pytestmark = pytest.mark.anyio

# The worker's error paths log with `log.exception`, and structlog's *default* renderer is
# the human console one, which writes raw non-ASCII to stdout. On a cp1252 console that
# raises UnicodeEncodeError out of the very handler reporting the failure, because this
# codebase's tracebacks quote source lines containing "ESD §…". `main()` calls this before
# anything else, so configuring it here tests the worker as it actually runs rather than in
# a console configuration it never sees.
configure_logging()


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class _Gateway:
    """Records calls; restart_pod succeeds, list_deployments is down.

    The second behaviour is what makes `scale_deployment` raise inside `execute_action`
    (it cannot resolve `current + delta` without the deployment list), which is the real
    exception path that exposed the batching defect.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.stopped = False

    async def call(self, server: str, tool: str, arguments: dict | None = None) -> dict:
        self.calls.append((server, tool))
        if tool == "restart_pod":
            return {"ok": True, "data": {"deleted": "checkout-abc"}}
        if tool == "list_deployments":
            return {"ok": False, "error_kind": "unavailable", "error": "k8s down"}
        return {"ok": True, "data": {}}

    async def stop(self) -> None:
        """Mirror `McpGateway.stop`, which graceful shutdown calls.

        The double drifted from the real interface when shutdown was added, and the failure
        surfaced as an unrelated-looking `AttributeError` deep in `Worker.stop`. Recording
        the call rather than silently passing means a future change that stops shutting the
        gateway down is visible here instead of leaking subprocesses.
        """
        self.stopped = True


async def _make_incident(session, state: IncidentState = IncidentState.open) -> Incident:
    incident = Incident(
        id=uuid.uuid4(),
        external_alert_id=f"rel-{uuid.uuid4().hex[:8]}",
        alert_source=AlertSource.prometheus,
        title="Checkout 5xx spike",
        service_name="checkout-service",
        severity=Severity.P1,
        state=state,
        alert_kind="error_rate",
        alert_value=0.7,
    )
    session.add(incident)
    await session.commit()
    return incident


async def _make_action(
    session,
    incident: Incident,
    *,
    action_type: str,
    parameters: dict,
    key: str,
) -> RemediationAction:
    action = RemediationAction(
        id=uuid.uuid4(),
        incident_id=incident.id,
        tier=2,
        action_type=action_type,
        target_resource_type="deployment",
        target_resource_id=f"deployment/{parameters.get('name', 'x')}",
        idempotency_key=key,
        parameters=parameters,
        compensating_action={"action_type": "noop"},
        reasoning="reliability test",
        blast_radius={"count": 0},
        status=RemediationStatus.approved,
        proposed_at=_now(),
        expires_at=_now() + datetime.timedelta(hours=1),
        shadow=False,
    )
    session.add(action)
    await session.commit()
    return action


def _worker(sessionmaker, gateway, worker_id: str = "worker-test") -> Worker:
    worker = Worker()
    worker.worker_id = worker_id
    worker.sessionmaker = sessionmaker
    worker.gateway = gateway
    return worker


# --- remediation execution atomicity -------------------------------------------------------


async def test_a_failing_action_does_not_roll_back_a_peer_that_already_ran(db_url, session):
    """One action's failure must not erase another action's real cluster mutation.

    Reproduced before the fix: `execute_approved_actions` ran the whole batch inside one
    transaction and committed once at the end. `restart_pod` executed against the cluster,
    then `scale_deployment` raised, and the rollback took the first action's row, its audit
    entry and its lease with it — leaving `status=approved` for a pod that had genuinely
    been deleted, so the next sweep deleted it again.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        incident = await _make_incident(session, state=IncidentState.hypothesis_formed)
        await _make_action(
            session,
            incident,
            action_type="restart_pod",
            parameters={"name": "checkout-abc"},
            key="rel-a-restart",
        )
        await _make_action(
            session,
            incident,
            action_type="scale_deployment",
            # No explicit `replicas`, so execution must resolve current+delta from a
            # deployment list this gateway reports as unavailable.
            parameters={"name": "checkout-service", "replicas_delta": 1},
            key="rel-b-scale",
        )

        gateway = _Gateway()
        worker = _worker(maker, gateway)
        # Must not raise: one bad action cannot abort the sweep for every other action.
        await worker.execute_approved_actions()

        assert ("k8s", "restart_pod") in gateway.calls, "the cluster write must have happened"

        async with maker() as s:
            rows = {
                r.idempotency_key: r
                for r in (await s.execute(select(RemediationAction))).scalars().all()
            }
            assert rows["rel-a-restart"].status == RemediationStatus.executed, (
                "an executed action must stay executed when a sibling action fails"
            )
            assert rows["rel-a-restart"].result is not None
            assert rows["rel-b-scale"].status == RemediationStatus.approved

            audits = (await s.execute(select(AuditLog))).scalars().all()
            assert any(a.action == "remediation_executed" for a in audits), (
                "the audit trail for a real remediation must survive a peer's failure"
            )
    finally:
        await engine.dispose()


async def test_execute_approved_actions_is_idempotent_across_two_sweeps(db_url, session):
    """Running the sweep twice must not execute the same approved action twice."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        incident = await _make_incident(session, state=IncidentState.hypothesis_formed)
        await _make_action(
            session,
            incident,
            action_type="restart_pod",
            parameters={"name": "checkout-abc"},
            key="rel-idem-restart",
        )
        gateway = _Gateway()
        worker = _worker(maker, gateway)

        await worker.execute_approved_actions()
        await worker.execute_approved_actions()

        restarts = [c for c in gateway.calls if c == ("k8s", "restart_pod")]
        assert len(restarts) == 1, f"action executed {len(restarts)} times across two sweeps"
    finally:
        await engine.dispose()


async def test_kill_switch_engaged_mid_sweep_stops_the_remaining_actions(db_url, session):
    """The kill switch must be re-read per action, not once per batch.

    Batching read `system_flags` through one session, so the identity map served the first
    action's answer to every later one: engaging the switch part-way through a sweep did
    not stop the actions still queued behind it. FR-5.3 requires the opposite — engaged
    means nothing executes anywhere, including work already approved.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from safety.kill_switch.switch import set_kill_switch

    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        incident = await _make_incident(session, state=IncidentState.hypothesis_formed)
        await _make_action(
            session,
            incident,
            action_type="restart_pod",
            parameters={"name": "pod-one"},
            key="rel-ks-one",
        )
        await _make_action(
            session,
            incident,
            action_type="restart_pod",
            parameters={"name": "pod-two"},
            key="rel-ks-two",
        )

        engaged = asyncio.Event()

        class _EngagingGateway(_Gateway):
            """Engages the kill switch during the first action's execution."""

            async def call(self, server: str, tool: str, arguments: dict | None = None) -> dict:
                result = await super().call(server, tool, arguments)
                if tool == "restart_pod" and not engaged.is_set():
                    engaged.set()
                    async with maker() as s:
                        await set_kill_switch(s, engaged=True, actor="test")
                        await s.commit()
                return result

        gateway = _EngagingGateway()
        worker = _worker(maker, gateway)
        await worker.execute_approved_actions()

        restarts = [c for c in gateway.calls if c == ("k8s", "restart_pod")]
        assert len(restarts) == 1, (
            "the kill switch was engaged after the first action; the second must not have run"
        )
    finally:
        async with maker() as s:
            from safety.kill_switch.switch import set_kill_switch as _reset

            await _reset(s, engaged=False, actor="test-teardown")
            await s.commit()
        await engine.dispose()


# --- investigation ownership over time -----------------------------------------------------


class _SlowGraph:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.invocations = 0

    async def ainvoke(self, state: dict, config: dict | None = None) -> dict:
        self.invocations += 1
        await asyncio.sleep(self.seconds)
        return state


async def test_a_long_investigation_keeps_its_claim(db_url, session, monkeypatch):
    """An investigation outliving STALE_AFTER must not have its incident stolen.

    Reproduced before the fix: the claim was written once and never refreshed, so any
    investigation longer than STALE_AFTER aged out its own lock while still running. A
    second worker then claimed the incident and ran a full parallel investigation —
    two hypotheses under one incident id and double the spend, which is precisely the
    defect migration 0008 was added to prevent.

    STALE_AFTER is compressed here; the heartbeat interval is derived from it, so the
    ratio under test is the same one that holds in production.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    monkeypatch.setattr(wm, "STALE_AFTER", datetime.timedelta(seconds=1))

    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        incident = await _make_incident(session)

        worker_a = _worker(maker, _Gateway(), worker_id="worker-a")
        worker_a.graph = _SlowGraph(3.0)
        worker_b = _worker(maker, _Gateway(), worker_id="worker-b")
        worker_b.graph = _SlowGraph(0.1)

        task_a = asyncio.create_task(worker_a.run_investigation(str(incident.id)))
        # Long enough for the un-refreshed lock to have expired under the old behaviour.
        await asyncio.sleep(2.0)
        await worker_b.run_investigation(str(incident.id))
        await task_a

        assert worker_a.graph.invocations == 1
        assert worker_b.graph.invocations == 0, (
            "a second worker investigated an incident that was still in flight"
        )
    finally:
        await engine.dispose()


async def test_a_crashed_worker_s_incident_is_still_reclaimable(db_url, session, monkeypatch):
    """The heartbeat must not defeat crash recovery.

    A dead worker stops beating, so its lock ages out and the ESD §4 sweep can re-run the
    incident. This is the property the heartbeat could plausibly have broken, so it is
    asserted directly rather than assumed.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    monkeypatch.setattr(wm, "STALE_AFTER", datetime.timedelta(seconds=1))

    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        incident = await _make_incident(session, state=IncidentState.investigating)
        # Exactly what a worker that died mid-investigation leaves behind: its own id on
        # the row, and a timestamp that no longer moves.
        incident.investigation_locked_by = "worker-dead"
        incident.investigation_locked_at = _now() - datetime.timedelta(seconds=30)
        await session.commit()

        survivor = _worker(maker, _Gateway(), worker_id="worker-survivor")
        survivor.graph = _SlowGraph(0.05)
        await survivor.run_investigation(str(incident.id))

        assert survivor.graph.invocations == 1, "a crashed worker's incident must be re-run"
    finally:
        await engine.dispose()


async def test_heartbeat_stops_refreshing_once_the_lock_is_taken_over(db_url, session, monkeypatch):
    """A worker that lost its claim must not refresh it back out from under the new owner."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    monkeypatch.setattr(wm, "STALE_AFTER", datetime.timedelta(seconds=1))

    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        incident = await _make_incident(session, state=IncidentState.investigating)
        incident.investigation_locked_by = "worker-new-owner"
        incident.investigation_locked_at = _now()
        await session.commit()

        loser = _worker(maker, _Gateway(), worker_id="worker-loser")
        lock_lost = asyncio.Event()
        task = asyncio.create_task(loser._heartbeat(str(incident.id), lock_lost))
        await asyncio.wait_for(lock_lost.wait(), timeout=5.0)
        task.cancel()

        async with maker() as s:
            row = (await s.execute(select(Incident).where(Incident.id == incident.id))).scalar_one()
            assert row.investigation_locked_by == "worker-new-owner", (
                "the losing worker must not have overwritten the new owner's claim"
            )
    finally:
        await engine.dispose()


async def test_releasing_a_lock_this_worker_no_longer_holds_is_a_no_op(db_url, session):
    """Release is idempotent and owner-scoped — running it twice changes nothing."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        incident = await _make_incident(session, state=IncidentState.investigating)
        incident.investigation_locked_by = "worker-other"
        incident.investigation_locked_at = _now()
        await session.commit()

        worker = _worker(maker, _Gateway(), worker_id="worker-not-owner")
        await worker._release_investigation_lock(str(incident.id))
        await worker._release_investigation_lock(str(incident.id))

        async with maker() as s:
            row = (await s.execute(select(Incident).where(Incident.id == incident.id))).scalar_one()
            assert row.investigation_locked_by == "worker-other"
    finally:
        await engine.dispose()


# --- worker lifecycle ------------------------------------------------------------------------


async def test_shutdown_releases_the_claims_of_cancelled_investigations(db_url, session):
    """A restart must not strand its in-flight incidents for a whole STALE_AFTER window."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        incident = await _make_incident(session)
        worker = _worker(maker, _Gateway(), worker_id="worker-restarting")
        worker.graph = _SlowGraph(30.0)

        worker._in_flight.add(str(incident.id))
        task = worker._spawn(worker.run_investigation(str(incident.id)))
        await asyncio.sleep(0.5)  # let the claim land

        async with maker() as s:
            row = (await s.execute(select(Incident).where(Incident.id == incident.id))).scalar_one()
            assert row.investigation_locked_by == "worker-restarting"

        await worker.stop(drain_timeout_seconds=0.2)
        assert task.cancelled() or task.done()

        async with maker() as s:
            row = (await s.execute(select(Incident).where(Incident.id == incident.id))).scalar_one()
            assert row.investigation_locked_by is None, (
                "shutdown must release claims so recovery does not wait out STALE_AFTER"
            )
    finally:
        await engine.dispose()
