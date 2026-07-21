"""Two-level circuit breaking for autonomous actions (FR-4.2, FR-12, ESD §17, §24).

Per-service: Tier-1 executions are rate-limited per service (default 3/hour); beyond the
limit, further Tier-1 actions are *promoted to Tier-2* (proposal + human approval), not
dropped — the system stays useful, the autonomy narrows.

Global: a rolling-window count of ALL Tier-1 executions system-wide. A single systemic
root cause (bad node, poisoned config push) must not let many individually-safe actions
form a collectively-risky wave. Tripping requires an admin to clear it (audited).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings
from core.logging import get_logger
from db.enums import RemediationStatus
from db.models import ActionCircuitBreakerEvent, Incident, RemediationAction


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def tier1_executions_for_service(
    session: AsyncSession, service_name: str, window: datetime.timedelta
) -> int:
    """Executed (non-shadow) Tier-1 actions for one service within the window."""
    cutoff = _now() - window
    stmt = (
        select(func.count())
        .select_from(RemediationAction)
        .join(Incident, Incident.id == RemediationAction.incident_id)
        .where(
            Incident.service_name == service_name,
            RemediationAction.tier == 1,
            RemediationAction.status == RemediationStatus.executed,
            RemediationAction.shadow.is_(False),
            RemediationAction.executed_at >= cutoff,
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def effective_tier(session: AsyncSession, service_name: str, proposed_tier: int) -> int:
    """FR-4.2: a Tier-1 action beyond the per-service rate limit is forced to Tier-2."""
    if proposed_tier != 1:
        return proposed_tier
    settings = get_settings()
    count = await tier1_executions_for_service(session, service_name, datetime.timedelta(hours=1))
    if count >= settings.tier1_rate_limit_per_hour:
        get_logger(component="circuit_breaker").warning(
            "tier1_rate_limit_reached", service=service_name, executed_last_hour=count
        )
        return 2
    return 1


async def _current_window(session: AsyncSession) -> ActionCircuitBreakerEvent:
    settings = get_settings()
    window_start_cutoff = _now() - datetime.timedelta(minutes=settings.breaker_window_minutes)
    row = (
        await session.execute(
            select(ActionCircuitBreakerEvent)
            .where(ActionCircuitBreakerEvent.window_start >= window_start_cutoff)
            .order_by(ActionCircuitBreakerEvent.window_start.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = ActionCircuitBreakerEvent(
            id=uuid.uuid4(), window_start=_now(), tier1_execution_count=0
        )
        session.add(row)
        await session.flush()
    return row


async def record_tier1_execution(session: AsyncSession) -> bool:
    """Count one Tier-1 execution against the global window. Returns True if this TRIPS it."""
    settings = get_settings()
    window = await _current_window(session)
    window.tier1_execution_count += 1
    if (
        window.tripped_at is None
        and window.tier1_execution_count >= settings.breaker_max_tier1_in_window
    ):
        window.tripped_at = _now()
        get_logger(component="circuit_breaker").error(
            "global_breaker_TRIPPED", count=window.tier1_execution_count
        )
        await session.flush()
        return True
    await session.flush()
    return False


async def is_globally_tripped(session: AsyncSession) -> bool:
    """True while any trip is uncleared — no Tier-1 auto-execution may proceed."""
    stmt = (
        select(func.count())
        .select_from(ActionCircuitBreakerEvent)
        .where(
            ActionCircuitBreakerEvent.tripped_at.is_not(None),
            ActionCircuitBreakerEvent.cleared_at.is_(None),
        )
    )
    return (await session.execute(stmt)).scalar_one() > 0


async def clear_global_breaker(session: AsyncSession, cleared_by: uuid.UUID) -> int:
    """Admin-only clear of all open trips (route enforces the role). Returns rows cleared."""
    result = await session.execute(
        update(ActionCircuitBreakerEvent)
        .where(
            ActionCircuitBreakerEvent.tripped_at.is_not(None),
            ActionCircuitBreakerEvent.cleared_at.is_(None),
        )
        .values(cleared_at=_now(), cleared_by=cleared_by)
    )
    return result.rowcount or 0
