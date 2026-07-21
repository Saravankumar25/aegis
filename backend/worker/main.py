"""Async worker: claims open incidents and drives the LangGraph investigation (ESD §4).

Wake-up is push-based: the ingestion commit's ``pg_notify`` lands here over a dedicated
LISTEN connection. A periodic sweep (default 60s) is the crash-recovery net — it also
performs the ESD §4 reconciliation: incidents stuck in ``investigating`` with no agent
activity for ``stale_after`` are re-run, which is safe because the MVP investigation is
read-only and idempotent (the deliberate no-checkpointer trade-off, ESD §25).

Run:  python -m worker.main
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import os
import socket
import uuid

import asyncpg
from sqlalchemy import func, select, text, update

from agents.gateway import McpGateway
from api.events import CHANNEL
from core.config import get_settings
from core.db import get_sessionmaker
from core.logging import configure_logging, get_logger
from core.tracing import trace_incident
from db.enums import ActorType, IncidentState
from db.models import AgentStep, Incident
from db.repository import IncidentRepository
from orchestrator.graph import InvestigationServices, build_graph
from providers.factory import get_provider

CONCURRENCY = 2
SWEEP_INTERVAL_SECONDS = 60
STALE_AFTER = datetime.timedelta(minutes=10)


class Worker:
    def __init__(self) -> None:
        # Identifies this worker in the incident's ownership column. Host plus pid plus a
        # random suffix: two replicas on one host, and a restarted process reusing a pid,
        # must not be mistaken for the same owner — the second case would let a fresh
        # worker adopt a lock it never took.
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"[:64]
        self.log = get_logger(component="worker")
        self.sessionmaker = get_sessionmaker()
        self.gateway = McpGateway()
        self.graph = None
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.semaphore = asyncio.Semaphore(CONCURRENCY)
        self._listen_conn: asyncpg.Connection | None = None
        self._in_flight: set[str] = set()

    async def start(self) -> None:
        await self.gateway.start()
        services = InvestigationServices(self.sessionmaker, self.gateway, get_provider())
        self.graph = build_graph(services)

        dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        self._listen_conn = await asyncpg.connect(dsn)
        await self._listen_conn.add_listener(CHANNEL, self._on_notify)
        self.log.info("worker_started", concurrency=CONCURRENCY)

        await self.reconcile()
        sweeper = asyncio.create_task(self._sweep_loop())
        try:
            while True:
                incident_id = await self.queue.get()
                asyncio.create_task(self._run_guarded(incident_id))
        finally:
            sweeper.cancel()

    def _on_notify(self, conn, pid, channel, payload: str) -> None:
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            return
        if event.get("event") == "incident_created":
            self.queue.put_nowait(event["incident_id"])

    async def _sweep_loop(self) -> None:
        """Crash-recovery net: pick up anything the NOTIFY path missed."""
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            with contextlib.suppress(Exception):
                await self.reconcile()
            with contextlib.suppress(Exception):
                await self.execute_approved_actions()

    async def execute_approved_actions(self) -> None:
        """V1.5: run approved (unexpired) Tier-2 actions through the gate-checked executor.

        Approval happens in the API process; execution stays in the worker (ESD §11:
        the API never acts on infrastructure inline).
        """
        from agents.resolution.engine import execute_action
        from db.enums import RemediationStatus
        from db.models import RemediationAction

        async with self.sessionmaker() as session:
            actions = (
                (
                    await session.execute(
                        select(RemediationAction)
                        .where(RemediationAction.status == RemediationStatus.approved)
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for action in actions:
                action = await execute_action(session, action, self.gateway, observer_approved=True)
                if action.status == RemediationStatus.executed and not action.shadow:
                    from agents.communication.writer import post_update

                    incident = await IncidentRepository(session).get(action.incident_id)
                    if incident is not None:
                        await post_update(
                            session,
                            self.gateway,
                            incident_id=incident.id,
                            phase="remediation_executed",
                            service=incident.service_name,
                            severity=str(incident.severity),
                            action_type=action.action_type,
                        )
            await session.commit()

    async def reconcile(self) -> None:
        """ESD §4 startup/periodic reconciliation: enqueue open + stale-investigating."""
        async with self.sessionmaker() as session:
            open_ids = (
                (
                    await session.execute(
                        select(Incident.id).where(Incident.state == IncidentState.open)
                    )
                )
                .scalars()
                .all()
            )
            cutoff = datetime.datetime.now(datetime.UTC) - STALE_AFTER
            last_step = (
                select(
                    AgentStep.incident_id,
                    func.max(AgentStep.created_at).label("last_at"),
                )
                .group_by(AgentStep.incident_id)
                .subquery()
            )
            stale_ids = (
                (
                    await session.execute(
                        select(Incident.id)
                        .outerjoin(last_step, last_step.c.incident_id == Incident.id)
                        .where(
                            Incident.state == IncidentState.investigating,
                            func.coalesce(last_step.c.last_at, Incident.updated_at) < cutoff,
                        )
                    )
                )
                .scalars()
                .all()
            )
        for incident_id in [*open_ids, *stale_ids]:
            self.queue.put_nowait(str(incident_id))
        if open_ids or stale_ids:
            self.log.info("reconcile_enqueued", open=len(open_ids), stale=len(stale_ids))

    async def _run_guarded(self, incident_id: str) -> None:
        if incident_id in self._in_flight:
            return
        self._in_flight.add(incident_id)
        try:
            async with self.semaphore:
                await self.run_investigation(incident_id)
        except Exception:
            # An unexpected failure must not kill the worker loop; the sweep retries later.
            self.log.exception("investigation_failed", incident_id=incident_id)
        finally:
            self._in_flight.discard(incident_id)

    async def run_investigation(self, incident_id: str) -> None:
        """Claim exclusive ownership, then drive the graph to completion.

        The row lock alone is not a claim. It is released at commit, and the graph then
        runs for a minute or more outside it — so a second worker taking the lock a moment
        later sees `investigating` and, because that state must stay claimable for
        crash recovery, proceeds to investigate the same incident. Ownership therefore has
        to be recorded on the row (migration 0008) and checked, not inferred from state.

        An incident is claimable when it is `open`, or `investigating` with a lock that is
        either absent or older than ``STALE_AFTER`` — the crashed-worker case the ESD §4
        sweep exists to recover.
        """
        async with self.sessionmaker() as session:
            row = (
                await session.execute(
                    select(Incident)
                    .where(Incident.id == uuid.UUID(incident_id))
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                # `skip_locked` returned nothing: another worker holds the row right now.
                return

            now = datetime.datetime.now(datetime.UTC)
            if row.state == IncidentState.open:
                await IncidentRepository(session).record_transition(
                    row,
                    IncidentState.investigating,
                    actor_type=ActorType.agent,
                    actor_id="worker",
                )
                await session.execute(
                    text("SELECT pg_notify(:c, :p)").bindparams(
                        c=CHANNEL,
                        p=json.dumps(
                            {
                                "incident_id": incident_id,
                                "event": "state_changed",
                                "data": {"state": "investigating"},
                            }
                        ),
                    )
                )
            elif row.state == IncidentState.investigating:
                held_by = row.investigation_locked_by
                held_at = row.investigation_locked_at
                owned_by_other = bool(held_by) and held_by != self.worker_id
                lock_is_fresh = held_at is not None and held_at > now - STALE_AFTER
                if owned_by_other and lock_is_fresh:
                    self.log.info(
                        "investigation_already_owned",
                        incident_id=incident_id,
                        owner=held_by,
                    )
                    return
            else:
                return  # already past investigation

            # Taken inside the same transaction as the state check, so the row lock makes
            # check-and-claim atomic against another worker doing the same thing.
            row.investigation_locked_by = self.worker_id
            row.investigation_locked_at = now
            state_snapshot = {
                "incident_id": incident_id,
                "service_name": row.service_name,
                "title": row.title,
                "severity": str(row.severity),
                "alert_kind": row.alert_kind or "other",
                "alert_value": row.alert_value,
                "revision_count": 0,
                "tokens_used": 0,
            }
            await session.commit()

        log = get_logger(incident_id=incident_id, component="worker")
        log.info("investigation_started", worker_id=self.worker_id)
        # One LangSmith trace per investigation, tagged with the same incident_id used
        # everywhere else, so a production incident is findable by the id an operator has.
        try:
            await self.graph.ainvoke(
                state_snapshot,
                config=trace_incident(incident_id, row.service_name, row.title),
            )
        finally:
            # Released on failure as well as success. Holding the lock after a crash would
            # be harmless (it ages out after STALE_AFTER) but it would delay recovery by
            # exactly that window for a failure the sweep could otherwise retry at once.
            await self._release_investigation_lock(incident_id)

    async def _release_investigation_lock(self, incident_id: str) -> None:
        """Drop this worker's claim, if it still holds it.

        Conditioned on `investigation_locked_by = self.worker_id` so a worker whose lock
        already aged out and was taken over cannot clear the new owner's claim on its way
        out — otherwise a slow worker finishing late would unlock an incident another
        worker is actively investigating, reintroducing the duplicate run.
        """
        with contextlib.suppress(Exception):
            async with self.sessionmaker() as session:
                await session.execute(
                    update(Incident)
                    .where(
                        Incident.id == uuid.UUID(incident_id),
                        Incident.investigation_locked_by == self.worker_id,
                    )
                    .values(investigation_locked_by=None, investigation_locked_at=None)
                )
                await session.commit()

    async def stop(self) -> None:
        if self._listen_conn is not None:
            with contextlib.suppress(Exception):
                await self._listen_conn.close()
        await self.gateway.stop()


async def main() -> None:
    configure_logging()
    worker = Worker()
    try:
        await worker.start()
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())
