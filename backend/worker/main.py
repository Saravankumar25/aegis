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
import uuid

import asyncpg
from sqlalchemy import func, select, text

from agents.gateway import McpGateway
from api.events import CHANNEL
from core.config import get_settings
from core.db import get_sessionmaker
from core.logging import configure_logging, get_logger
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
                    from agents.communication.composer import post_update

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
        """Claim (open→investigating under row lock) and drive the graph to completion."""
        async with self.sessionmaker() as session:
            row = (
                await session.execute(
                    select(Incident)
                    .where(Incident.id == uuid.UUID(incident_id))
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return
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
            elif row.state != IncidentState.investigating:
                return  # already past investigation
            state_snapshot = {
                "incident_id": incident_id,
                "service_name": row.service_name,
                "title": row.title,
                "severity": str(row.severity),
                "revision_count": 0,
                "tokens_used": 0,
            }
            await session.commit()

        log = get_logger(incident_id=incident_id, component="worker")
        log.info("investigation_started")
        await self.graph.ainvoke(state_snapshot)

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
