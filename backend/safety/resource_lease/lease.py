"""Resource leasing: at most one in-flight action per infrastructure target (ESD §24).

The invariant lives in the database — the partial unique index on
``(target_resource_type, target_resource_id) WHERE released_at IS NULL`` — so two workers
racing to act on the same deployment cannot both win, no matter what the application code
believes. ``acquire`` returns None on contention instead of raising: the caller treats a
held lease as "someone else is already handling this target".
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import ResourceLease


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def acquire_lease(
    session: AsyncSession,
    *,
    target_resource_type: str,
    target_resource_id: str,
    remediation_action_id: uuid.UUID,
) -> ResourceLease | None:
    """Try to take the active lease on a target. None = already held by someone else."""
    lease_id = uuid.uuid4()
    stmt = (
        pg_insert(ResourceLease)
        .values(
            id=lease_id,
            target_resource_type=target_resource_type,
            target_resource_id=target_resource_id,
            held_by_remediation_action_id=remediation_action_id,
            acquired_at=_now(),
            released_at=None,
        )
        .on_conflict_do_nothing(
            index_elements=["target_resource_type", "target_resource_id"],
            index_where=text("released_at IS NULL"),
        )
        .returning(ResourceLease.id)
    )
    inserted = (await session.execute(stmt)).scalar_one_or_none()
    if inserted is None:
        return None
    return await session.get(ResourceLease, inserted)


async def release_lease(session: AsyncSession, lease_id: uuid.UUID) -> None:
    """Release a held lease (idempotent: releasing twice is a no-op)."""
    await session.execute(
        update(ResourceLease)
        .where(ResourceLease.id == lease_id, ResourceLease.released_at.is_(None))
        .values(released_at=_now())
    )


async def release_leases_for_action(
    session: AsyncSession, remediation_action_id: uuid.UUID
) -> None:
    """Release every active lease held by one remediation action (cleanup path)."""
    await session.execute(
        update(ResourceLease)
        .where(
            ResourceLease.held_by_remediation_action_id == remediation_action_id,
            ResourceLease.released_at.is_(None),
        )
        .values(released_at=_now())
    )
