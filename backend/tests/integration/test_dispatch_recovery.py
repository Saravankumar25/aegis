"""Durability properties that require real Postgres locking and transactions."""

from __future__ import annotations

import pytest

from aegis.persistence.db import Database
from aegis.persistence.dispatch_outbox import DISPATCH_LEASE_SECONDS, DispatchOutbox
from aegis.persistence.jobs import LEASE_SECONDS, JobQueue

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_committed_job_keeps_one_dispatch_request(db: Database, clean_incident: str) -> None:
    queue = JobQueue(db)
    job_id = await queue.enqueue(incident_id=clean_incident, kind="dispatch_test")
    duplicate = await queue.enqueue(incident_id=clean_incident, kind="dispatch_test")

    assert job_id is not None and duplicate is None
    counts = await db.fetchrow(
        """
        SELECT
          (SELECT count(*) FROM workflow_jobs WHERE id=$1) AS jobs,
          (SELECT count(*) FROM workflow_dispatch_outbox WHERE job_id=$1) AS dispatches
        """,
        job_id,
    )
    assert counts is not None
    assert counts["jobs"] == 1 and counts["dispatches"] == 1


async def test_stale_dispatch_and_job_are_recoverable_without_losing_checkpoint(
    db: Database, clean_incident: str
) -> None:
    queue = JobQueue(db)
    job_id = await queue.enqueue(incident_id=clean_incident, kind="recovery_test")
    assert job_id is not None
    job = await queue.claim("dead-worker", kinds=["recovery_test"])
    dispatch = await DispatchOutbox(db).claim("dead-dispatcher", "postgres")
    assert job is not None and dispatch is not None

    run_id = f"run_{job_id}"
    await db.execute(
        """
        INSERT INTO horizon_checkpoints (run_id, incident_id, step, phase, state)
        VALUES ($2, $1, 3, 'INVESTIGATE', '{"step": 3}'::jsonb)
        """,
        clean_incident,
        run_id,
    )
    await db.execute(
        "UPDATE workflow_jobs SET locked_at=now()-make_interval(secs=>$2) WHERE id=$1",
        job_id,
        LEASE_SECONDS + 1,
    )
    await db.execute(
        """
        UPDATE workflow_dispatch_outbox
           SET locked_at=now()-make_interval(secs=>$2) WHERE id=$1
        """,
        dispatch.id,
        DISPATCH_LEASE_SECONDS + 1,
    )

    assert await queue.reap_stale() >= 1
    assert await DispatchOutbox(db).recover_stale() >= 1
    assert await db.fetchval("SELECT status FROM workflow_jobs WHERE id=$1", job_id) == "queued"
    assert (
        await db.fetchval("SELECT status FROM workflow_dispatch_outbox WHERE id=$1", dispatch.id)
        == "pending"
    )
    assert await db.fetchval("SELECT step FROM horizon_checkpoints WHERE run_id=$1", run_id) == 3
    await db.execute("DELETE FROM horizon_checkpoints WHERE run_id=$1", run_id)


async def test_stale_worker_cannot_settle_a_reclaimed_job(
    db: Database, clean_incident: str
) -> None:
    queue = JobQueue(db)
    job_id = await queue.enqueue(incident_id=clean_incident, kind="fencing_test")
    assert job_id is not None
    first = await queue.claim("worker-a", kinds=["fencing_test"])
    assert first is not None

    await db.execute(
        "UPDATE workflow_jobs SET locked_at=now()-make_interval(secs=>$2) WHERE id=$1",
        job_id,
        LEASE_SECONDS + 1,
    )
    assert await queue.reap_stale() == 1
    second = await queue.claim("worker-b", kinds=["fencing_test"])
    assert second is not None
    assert second["lease_version"] > first["lease_version"]

    assert not await queue.complete(
        job_id,
        worker_id="worker-a",
        lease_version=first["lease_version"],
    )
    assert not await queue.fail(
        job_id,
        "stale failure",
        worker_id="worker-a",
        lease_version=first["lease_version"],
    )
    assert await queue.complete(
        job_id,
        worker_id="worker-b",
        lease_version=second["lease_version"],
    )


async def test_stale_dispatcher_cannot_settle_a_reclaimed_dispatch(
    db: Database, clean_incident: str
) -> None:
    job_id = await JobQueue(db).enqueue(incident_id=clean_incident, kind="dispatch_fencing_test")
    assert job_id is not None
    outbox = DispatchOutbox(db)
    first = await outbox.claim("dispatcher-a", "postgres")
    assert first is not None

    await db.execute(
        """
        UPDATE workflow_dispatch_outbox
           SET locked_at=now()-make_interval(secs=>$2) WHERE id=$1
        """,
        first.id,
        DISPATCH_LEASE_SECONDS + 1,
    )
    assert await outbox.recover_stale() == 1
    second = await outbox.claim("dispatcher-b", "postgres")
    assert second is not None
    assert second.lease_version > first.lease_version

    assert not await outbox.mark_dispatched(
        first.id,
        "stale:result",
        dispatcher_id="dispatcher-a",
        lease_version=first.lease_version,
    )
    assert await outbox.mark_dispatched(
        second.id,
        "postgres:job",
        dispatcher_id="dispatcher-b",
        lease_version=second.lease_version,
    )


async def test_step_functions_job_is_not_claimable_by_local_worker(
    db: Database, clean_incident: str
) -> None:
    queue = JobQueue(db, dispatch_destination="step_functions")
    job_id = await queue.enqueue(incident_id=clean_incident, kind="remote_only_test")
    assert job_id is not None

    assert await queue.claim("local-worker", kinds=["remote_only_test"]) is None
    remote = await queue.claim(
        "step-functions-worker",
        kinds=["remote_only_test"],
        dispatch_destination="step_functions",
    )
    assert remote is not None and remote["id"] == job_id
    assert await queue.complete(
        job_id,
        worker_id="step-functions-worker",
        lease_version=remote["lease_version"],
    )
