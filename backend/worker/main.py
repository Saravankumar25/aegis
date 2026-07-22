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
import signal
import socket
import uuid

import asyncpg
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import SQLAlchemyError

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
# The claim must be re-proven many times over before it could expire, so one missed beat
# (a database blip) never costs a running investigation its owner.
HEARTBEAT_FRACTION_OF_STALE_AFTER = 0.1


def heartbeat_interval_seconds() -> float:
    """How often a running investigation re-proves its claim.

    Derived from ``STALE_AFTER`` rather than set independently so the invariant that
    matters — a live investigation always refreshes its lock well before it expires —
    cannot be broken by someone tuning one constant and not the other.
    """
    return STALE_AFTER.total_seconds() * HEARTBEAT_FRACTION_OF_STALE_AFTER


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
        # Strong references to every spawned investigation. `asyncio.create_task` only holds
        # a weak reference, so a task nobody keeps can be garbage-collected mid-await and the
        # investigation simply stops — with no exception and no log line. The set is also what
        # graceful shutdown drains.
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopping = asyncio.Event()

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
            while not self._stopping.is_set():
                incident_id = await self._next_incident()
                if incident_id is None:
                    break
                self._spawn(self._run_guarded(incident_id))
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper

    async def _next_incident(self) -> str | None:
        """Next queued incident, or None once shutdown has been requested.

        Racing the queue against the stop event is what makes SIGTERM take effect promptly:
        a bare ``await queue.get()`` parks forever on an idle worker, so the process would
        only notice the signal the next time an alert happened to arrive.
        """
        getter = asyncio.ensure_future(self.queue.get())
        stopper = asyncio.ensure_future(self._stopping.wait())
        done, _ = await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if getter in done:
            stopper.cancel()
            return getter.result()
        # Shutting down: cancel the pending get, but hand back anything it had already taken
        # off the queue so a signal cannot silently drop an incident.
        getter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            return getter.result()
        return None

    def _spawn(self, coro) -> asyncio.Task[None]:
        """Create a task and keep a strong reference until it finishes."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _on_notify(self, conn, pid, channel, payload: str) -> None:
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            self.log.warning("worker_bad_notify_payload")
            return
        incident_id = event.get("incident_id")
        # `.get` rather than `[...]`: this runs inside asyncpg's listener callback, where a
        # KeyError from one malformed payload is not attributable to anything a reader of the
        # logs could act on.
        if event.get("event") == "incident_created" and incident_id:
            self.queue.put_nowait(str(incident_id))

    async def _sweep_loop(self) -> None:
        """Crash-recovery net: pick up anything the NOTIFY path missed.

        This loop is the last line of defence, so it is written to be un-killable. It
        previously wrapped each pass in `contextlib.suppress(Exception)`, which had two
        distinct problems: a sweep failing every single cycle — including one unable to
        execute approved remediations — looked exactly like a sweep with nothing to do, and
        any exception raised outside those two suppressors would end the task outright. A
        dead sweeper is silent, because nothing awaits this task until shutdown, and it is
        precisely what recovers a crashed peer's incidents.
        """
        while not self._stopping.is_set():
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            # Broad by design: a supervisor loop is the one place a catch-all is correct,
            # because the alternative to handling an unforeseen error is losing recovery
            # entirely. It is logged with a traceback, never swallowed.
            try:
                await self.reconcile()
            except Exception:
                self.log.exception("reconcile_failed")
            try:
                await self.execute_approved_actions()
            except Exception:
                self.log.exception("execute_approved_actions_failed")

    async def execute_approved_actions(self) -> None:
        """V1.5: run approved (unexpired) Tier-2 actions through the gate-checked executor.

        Approval happens in the API process; execution stays in the worker (ESD §11:
        the API never acts on infrastructure inline).

        **One action, one transaction.** This previously ran the whole batch inside a single
        transaction that committed once at the end, which meant an exception raised while
        executing the *second* action rolled back the *first* — after its MCP call had
        already changed the cluster. The row then read `approved` again, the audit entry
        recording a real remediation was gone, the Tier-1 breaker count that governs how
        much autonomy is left was un-incremented, and the next sweep re-executed it. Since
        the whole sweep was wrapped in a blanket exception suppressor, none of that was
        visible. Committing per action also re-reads the kill switch from the database
        before every action instead of once per batch, so engaging it mid-sweep stops the
        actions that have not run yet.
        """
        from agents.communication.writer import post_update
        from agents.resolution.engine import execute_action
        from db.enums import RemediationStatus
        from db.models import RemediationAction

        async with self.sessionmaker() as session:
            candidate_ids = (
                (
                    await session.execute(
                        select(RemediationAction.id).where(
                            RemediationAction.status == RemediationStatus.approved
                        )
                    )
                )
                .scalars()
                .all()
            )

        for action_id in candidate_ids:
            try:
                await self._execute_one_action(action_id, execute_action, post_update)
            except Exception:
                # Scoped to one action deliberately: a single bad action must not strand
                # every other approved remediation waiting behind it, and it must be
                # attributable in the logs rather than swallowed by the sweep.
                self.log.exception("remediation_execution_failed", action_id=str(action_id))

    async def _execute_one_action(self, action_id: uuid.UUID, execute_action, post_update) -> None:
        """Execute exactly one approved action in its own transaction."""
        from db.enums import RemediationStatus
        from db.models import RemediationAction

        async with self.sessionmaker() as session:
            action = (
                await session.execute(
                    select(RemediationAction)
                    .where(
                        RemediationAction.id == action_id,
                        # Re-checked while holding the row lock, not just when the candidate
                        # list was built: in between, another worker may have executed this
                        # action or a human may have rejected it.
                        RemediationAction.status == RemediationStatus.approved,
                    )
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if action is None:
                return
            action = await execute_action(session, action, self.gateway, observer_approved=True)
            executed = action.status == RemediationStatus.executed and not action.shadow
            incident_id = action.incident_id
            action_type = action.action_type
            await session.commit()

        if not executed:
            return
        # Announced only after the execution is durably recorded. A stakeholder update is a
        # claim that something happened to production; sending it from inside the same
        # transaction meant it could be sent and then rolled back.
        async with self.sessionmaker() as session:
            incident = await IncidentRepository(session).get(incident_id)
            if incident is None:
                return
            await post_update(
                session,
                self.gateway,
                incident_id=incident.id,
                phase="remediation_executed",
                service=incident.service_name,
                severity=str(incident.severity),
                action_type=action_type,
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
        lock_lost = asyncio.Event()
        heartbeat = self._spawn(self._heartbeat(incident_id, lock_lost))
        try:
            await self._run_graph_until_lock_lost(
                state_snapshot,
                trace_incident(incident_id, row.service_name, row.title),
                lock_lost,
                log,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            # Released on failure as well as success. Holding the lock after a crash would
            # be harmless (it ages out after STALE_AFTER) but it would delay recovery by
            # exactly that window for a failure the sweep could otherwise retry at once.
            await self._release_investigation_lock(incident_id)

    async def _run_graph_until_lock_lost(
        self,
        state_snapshot: dict,
        config: dict,
        lock_lost: asyncio.Event,
        log,
    ) -> None:
        """Drive the graph, abandoning the run if this worker stops owning the incident.

        Losing the lock means another worker has already taken the incident over, so
        continuing would produce the duplicate investigation — two hypotheses under one
        incident id, double the spend — that ownership exists to prevent. Abandoning is
        safe because each agent step commits its own transaction, so the work already
        done is durable and the new owner picks up from a consistent record.
        """
        graph_task = asyncio.ensure_future(self.graph.ainvoke(state_snapshot, config=config))
        lost_task = asyncio.ensure_future(lock_lost.wait())
        try:
            done, _ = await asyncio.wait(
                {graph_task, lost_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if graph_task in done:
                graph_task.result()  # re-raise anything the investigation failed on
                return
            log.warning("investigation_abandoned_lock_lost", worker_id=self.worker_id)
            graph_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await graph_task
        finally:
            lost_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await lost_task

    async def _heartbeat(self, incident_id: str, lock_lost: asyncio.Event) -> None:
        """Re-prove ownership periodically for as long as the investigation runs.

        The claim records *when ownership was last proven*, and migration 0008 wrote it
        once at claim time and never again. Any investigation lasting longer than
        ``STALE_AFTER`` therefore aged its own lock out while still running, the
        reconciliation sweep saw a stale lock, and a second worker took the incident over —
        reintroducing exactly the concurrent-investigation defect the lock was added for.
        """
        while True:
            await asyncio.sleep(heartbeat_interval_seconds())
            try:
                async with self.sessionmaker() as session:
                    result = await session.execute(
                        update(Incident)
                        .where(
                            Incident.id == uuid.UUID(incident_id),
                            # Conditioned on ownership: a worker that already lost the lock
                            # must not be able to refresh it back out from under the new
                            # owner. A zero rowcount is how it learns it lost it.
                            Incident.investigation_locked_by == self.worker_id,
                        )
                        .values(investigation_locked_at=datetime.datetime.now(datetime.UTC))
                    )
                    await session.commit()
                if result.rowcount == 0:
                    self.log.warning(
                        "investigation_lock_lost",
                        incident_id=incident_id,
                        worker_id=self.worker_id,
                    )
                    lock_lost.set()
                    return
            except (SQLAlchemyError, OSError) as exc:
                # A transient database blip must not abort a healthy investigation. The
                # next beat retries and STALE_AFTER is many beats wide, so only a sustained
                # outage can cost this worker its claim.
                self.log.warning(
                    "investigation_heartbeat_failed", incident_id=incident_id, error=str(exc)
                )

    async def _release_investigation_lock(self, incident_id: str) -> None:
        """Drop this worker's claim, if it still holds it.

        Conditioned on `investigation_locked_by = self.worker_id` so a worker whose lock
        already aged out and was taken over cannot clear the new owner's claim on its way
        out — otherwise a slow worker finishing late would unlock an incident another
        worker is actively investigating, reintroducing the duplicate run.

        Idempotent: releasing a lock this worker no longer holds updates zero rows.
        """
        try:
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
        except (SQLAlchemyError, OSError) as exc:
            # Failing to release is recoverable — the lock ages out after STALE_AFTER — but
            # it delays recovery by that window, so it must not be invisible.
            self.log.warning(
                "investigation_lock_release_failed", incident_id=incident_id, error=str(exc)
            )

    def request_stop(self) -> None:
        """Signal-handler-safe shutdown request (must not await anything)."""
        if not self._stopping.is_set():
            self.log.info("worker_stopping")
            self._stopping.set()

    async def stop(self, *, drain_timeout_seconds: float = 30.0) -> None:
        """Drain in-flight investigations, then release the resources they held.

        Draining matters because of what the alternative costs: killing the process with
        investigations in flight leaves their claims held, and a held claim is only
        reclaimable after ``STALE_AFTER``. Every restart would therefore stall recovery of
        whatever was running by ten minutes. Investigations that do not finish inside the
        drain window are cancelled and their claims released explicitly, which is the same
        outcome far sooner.
        """
        self._stopping.set()
        if self._tasks:
            pending = set(self._tasks)
            done, still_running = await asyncio.wait(pending, timeout=drain_timeout_seconds)
            for task in still_running:
                task.cancel()
            if still_running:
                self.log.warning("worker_drain_timeout", cancelled=len(still_running))
                await asyncio.gather(*still_running, return_exceptions=True)
        # Belt and braces: a cancelled investigation's own `finally` may itself be cut short
        # by the cancellation it is unwinding, so the claims are cleared here too. Releasing
        # a lock this worker no longer owns is a no-op — the UPDATE is owner-conditioned.
        for incident_id in list(self._in_flight):
            await self._release_investigation_lock(incident_id)
        if self._listen_conn is not None:
            with contextlib.suppress(Exception):
                await self._listen_conn.close()
            self._listen_conn = None
        await self.gateway.stop()


async def main() -> None:
    configure_logging()
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except (NotImplementedError, AttributeError, ValueError):
            # Windows' proactor loop implements neither; the KeyboardInterrupt path still
            # reaches the `finally` below, so shutdown degrades to the same drain.
            with contextlib.suppress(OSError, ValueError):
                signal.signal(sig, lambda *_: worker.request_stop())
    try:
        await worker.start()
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(main())
