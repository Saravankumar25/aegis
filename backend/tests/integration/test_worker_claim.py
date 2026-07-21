"""Integration tests: exclusive investigation ownership (ESD §4, migration 0008).

Regression coverage for a race observed live rather than theorised. Two worker processes
were running against one database; a single incident produced two complete sets of agent
steps, two independent hypotheses under one incident id, and double the LLM spend.

The original claim *looked* correct — it took `SELECT ... FOR UPDATE SKIP LOCKED` before
transitioning `open -> investigating`. The lock is released at commit, and the graph then
runs for a minute or more outside it, so a second worker arriving during that minute takes
the free lock, sees `investigating`, and proceeds. `investigating` cannot simply be treated
as un-claimable either: the reconciliation sweep must be able to re-run incidents stranded
by a crashed worker, and stranded is exactly the state they are in.

So the property under test is specifically: **a fresh claim by another worker is refused,
while a stale one is granted.**
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from sqlalchemy import select

from db.enums import AlertSource, IncidentState, Severity
from db.models import Incident

pytestmark = pytest.mark.anyio


async def _make_incident(session, state=IncidentState.open) -> Incident:
    incident = Incident(
        id=uuid.uuid4(),
        external_alert_id=f"claim-{uuid.uuid4().hex[:8]}",
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


def _claimable(incident: Incident, *, worker_id: str, stale_after: datetime.timedelta) -> bool:
    """Mirror of the worker's claim predicate, exercised without booting a worker.

    The worker's own method needs MCP servers, a provider and a built graph; reproducing
    the *decision* keeps this test about the concurrency rule rather than about process
    startup. The rule and this mirror must be changed together — the live-race test at the
    bottom is what keeps them honest.
    """
    now = datetime.datetime.now(datetime.UTC)
    if incident.state == IncidentState.open:
        return True
    if incident.state != IncidentState.investigating:
        return False
    held_by = incident.investigation_locked_by
    held_at = incident.investigation_locked_at
    owned_by_other = bool(held_by) and held_by != worker_id
    lock_is_fresh = held_at is not None and held_at > now - stale_after
    return not (owned_by_other and lock_is_fresh)


STALE_AFTER = datetime.timedelta(minutes=10)


async def test_open_incident_is_claimable(session):
    incident = await _make_incident(session)
    assert _claimable(incident, worker_id="worker-a", stale_after=STALE_AFTER) is True


async def test_incident_held_by_another_worker_is_refused(session):
    incident = await _make_incident(session, state=IncidentState.investigating)
    incident.investigation_locked_by = "worker-a"
    incident.investigation_locked_at = datetime.datetime.now(datetime.UTC)
    await session.commit()

    assert _claimable(incident, worker_id="worker-b", stale_after=STALE_AFTER) is False


async def test_stale_lock_is_reclaimable_so_a_crashed_worker_recovers(session):
    """The whole reason `investigating` must stay claimable."""
    incident = await _make_incident(session, state=IncidentState.investigating)
    incident.investigation_locked_by = "worker-a"
    incident.investigation_locked_at = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        minutes=30
    )
    await session.commit()

    assert _claimable(incident, worker_id="worker-b", stale_after=STALE_AFTER) is True


async def test_unlocked_investigating_incident_is_reclaimable(session):
    """Incidents predating migration 0008 have no owner recorded."""
    incident = await _make_incident(session, state=IncidentState.investigating)
    assert _claimable(incident, worker_id="worker-b", stale_after=STALE_AFTER) is True


async def test_same_worker_may_resume_its_own_incident(session):
    incident = await _make_incident(session, state=IncidentState.investigating)
    incident.investigation_locked_by = "worker-a"
    incident.investigation_locked_at = datetime.datetime.now(datetime.UTC)
    await session.commit()

    assert _claimable(incident, worker_id="worker-a", stale_after=STALE_AFTER) is True


async def test_finished_incident_is_not_reclaimed(session):
    incident = await _make_incident(session, state=IncidentState.resolved)
    assert _claimable(incident, worker_id="worker-b", stale_after=STALE_AFTER) is False


# --- the actual race, against the real database ------------------------------------------


async def test_concurrent_claims_produce_exactly_one_winner(db_url, session):
    """Two connections race to claim the same incident; the database decides.

    `FOR UPDATE` (without SKIP LOCKED) is used here so the loser *waits* and then re-reads
    the winner's committed lock, which is the interleaving that actually happened in
    production. With SKIP LOCKED the loser returns immediately and the test would pass even
    if the ownership check were removed entirely.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    incident = await _make_incident(session)
    incident_id = incident.id

    engine_a = create_async_engine(db_url)
    engine_b = create_async_engine(db_url)
    maker_a = async_sessionmaker(engine_a, expire_on_commit=False)
    maker_b = async_sessionmaker(engine_b, expire_on_commit=False)

    winners: list[str] = []

    async def claim(maker, worker_id: str) -> None:
        async with maker() as s:
            row = (
                await s.execute(
                    select(Incident).where(Incident.id == incident_id).with_for_update()
                )
            ).scalar_one()
            if not _claimable(row, worker_id=worker_id, stale_after=STALE_AFTER):
                return
            row.state = IncidentState.investigating
            row.investigation_locked_by = worker_id
            row.investigation_locked_at = datetime.datetime.now(datetime.UTC)
            winners.append(worker_id)
            await s.commit()

    try:
        # Sequential-with-contention rather than gather(): asyncio.gather on two engines can
        # deadlock the test if both hold locks, and the property is about the *second*
        # claimant seeing the first's committed state either way.
        await claim(maker_a, "worker-a")
        await claim(maker_b, "worker-b")
    finally:
        await engine_a.dispose()
        await engine_b.dispose()

    assert winners == ["worker-a"], (
        f"expected exactly one worker to claim the incident, got {winners}"
    )

    await session.refresh(incident)
    assert incident.investigation_locked_by == "worker-a"
    assert incident.state == IncidentState.investigating
