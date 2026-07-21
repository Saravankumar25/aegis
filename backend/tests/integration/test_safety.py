"""Integration tests for the V1.5 safety substrate (CLAUDE.md §10: safety = tested).

Leases race against the real partial unique index; breakers count real rows; the kill
switch round-trips through system_flags.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.enums import AlertSource, RemediationStatus, Severity
from db.models import Incident, RemediationAction
from db.repository import IncidentRepository
from safety.circuit_breaker.breaker import (
    clear_global_breaker,
    effective_tier,
    is_globally_tripped,
    record_tier1_execution,
)
from safety.kill_switch.switch import is_kill_switch_engaged, set_kill_switch
from safety.resource_lease.lease import acquire_lease, release_lease


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def _make_incident(session: AsyncSession, service: str = "checkout-service") -> Incident:
    incident, _ = await IncidentRepository(session).upsert_incident(
        external_alert_id=f"safety-{uuid.uuid4().hex[:8]}",
        alert_source=AlertSource.prometheus,
        title="safety test",
        service_name=service,
        severity=Severity.P2,
    )
    await session.commit()
    return incident


async def _make_action(
    session: AsyncSession,
    incident: Incident,
    *,
    tier: int = 1,
    status: RemediationStatus = RemediationStatus.proposed,
    executed_at: datetime.datetime | None = None,
) -> RemediationAction:
    action = RemediationAction(
        incident_id=incident.id,
        tier=tier,
        action_type="restart_pod",
        target_resource_type="pod",
        target_resource_id=f"meridian/pod/{uuid.uuid4().hex[:8]}",
        idempotency_key=uuid.uuid4().hex,
        parameters={},
        compensating_action={"action_type": "none", "note": "restart has no undo"},
        reasoning="test",
        blast_radius={"dependents": []},
        status=status,
        proposed_at=_now(),
        expires_at=_now() + datetime.timedelta(minutes=30),
        executed_at=executed_at,
        shadow=False,
    )
    session.add(action)
    await session.commit()
    return action


# --- resource leases -------------------------------------------------------------------


async def test_second_lease_on_same_target_is_refused(session: AsyncSession):
    incident = await _make_incident(session)
    a1 = await _make_action(session, incident)
    a2 = await _make_action(session, incident)
    lease1 = await acquire_lease(
        session,
        target_resource_type="deployment",
        target_resource_id="meridian/checkout-service",
        remediation_action_id=a1.id,
    )
    assert lease1 is not None
    lease2 = await acquire_lease(
        session,
        target_resource_type="deployment",
        target_resource_id="meridian/checkout-service",
        remediation_action_id=a2.id,
    )
    assert lease2 is None  # the database said no, not the application

    await release_lease(session, lease1.id)
    lease3 = await acquire_lease(
        session,
        target_resource_type="deployment",
        target_resource_id="meridian/checkout-service",
        remediation_action_id=a2.id,
    )
    assert lease3 is not None  # released → acquirable again


async def test_concurrent_lease_race_has_exactly_one_winner(db_url: str, session: AsyncSession):
    incident = await _make_incident(session)
    actions = [await _make_action(session, incident) for _ in range(4)]
    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def contend(action_id: uuid.UUID) -> bool:
        async with maker() as s:
            lease = await acquire_lease(
                s,
                target_resource_type="pod",
                target_resource_id="meridian/pod/contended",
                remediation_action_id=action_id,
            )
            await s.commit()
            return lease is not None

    results = await asyncio.gather(*(contend(a.id) for a in actions))
    await engine.dispose()
    assert sum(results) == 1  # exactly one winner, three losers


# --- circuit breakers ------------------------------------------------------------------


async def test_tier1_rate_limit_promotes_to_tier2(session: AsyncSession):
    incident = await _make_incident(session, "payment-service")
    assert await effective_tier(session, "payment-service", 1) == 1
    for _ in range(3):  # default limit = 3/h
        await _make_action(session, incident, status=RemediationStatus.executed, executed_at=_now())
    assert await effective_tier(session, "payment-service", 1) == 2  # FR-4.2
    # Other services are unaffected (per-service isolation).
    assert await effective_tier(session, "catalog-service", 1) == 1
    # Non-Tier-1 tiers pass through untouched.
    assert await effective_tier(session, "payment-service", 3) == 3


async def test_global_breaker_trips_and_requires_clear(session: AsyncSession):
    from core.config import get_settings

    threshold = get_settings().breaker_max_tier1_in_window
    assert not await is_globally_tripped(session)
    tripped = False
    for _ in range(threshold):
        tripped = await record_tier1_execution(session)
    assert tripped is True
    assert await is_globally_tripped(session)

    admin_id = uuid.uuid4()
    from db.enums import UserRole
    from db.models import User

    session.add(
        User(
            id=admin_id,
            email="breaker-admin@example.com",
            firebase_uid="uid-breaker-admin",
            role=UserRole.admin,
        )
    )
    await session.flush()
    cleared = await clear_global_breaker(session, cleared_by=admin_id)
    assert cleared == 1
    assert not await is_globally_tripped(session)


# --- kill switch -----------------------------------------------------------------------


async def test_kill_switch_round_trip(session: AsyncSession):
    assert not await is_kill_switch_engaged(session)
    await set_kill_switch(session, engaged=True, actor="admin@example.com")
    assert await is_kill_switch_engaged(session)
    await set_kill_switch(session, engaged=False, actor="admin@example.com")
    assert not await is_kill_switch_engaged(session)
