-- Durable dispatch outbox for scale-to-zero workflow launch.
--
-- A row is inserted in the same transaction as workflow_jobs.  Dispatch to an
-- external orchestrator happens only after commit, and a crashed dispatcher can
-- safely reclaim the row.  The job id is the idempotency key at every adapter.

ALTER TABLE workflow_jobs
    ADD COLUMN IF NOT EXISTS lease_version BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS workflow_dispatch_outbox (
    id             TEXT PRIMARY KEY,
    job_id         TEXT NOT NULL UNIQUE REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    incident_id    TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    destination    TEXT NOT NULL DEFAULT 'postgres'
        CHECK (destination IN ('postgres', 'step_functions')),
    status         TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'dispatching', 'dispatched', 'failed')),
    attempts       INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts   INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    locked_by      TEXT,
    locked_at      TIMESTAMPTZ,
    run_after      TIMESTAMPTZ NOT NULL DEFAULT now(),
    external_ref   TEXT,
    last_error     TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    dispatched_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS workflow_dispatch_outbox_pending_idx
    ON workflow_dispatch_outbox (destination, status, run_after)
    WHERE status IN ('pending', 'dispatching');

-- Make the invariant structural for every present and future workflow_jobs
-- writer. JobQueue updates the destination in the same transaction when the
-- deployment opts into Step Functions.
CREATE OR REPLACE FUNCTION aegis_create_dispatch_outbox()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO workflow_dispatch_outbox (id, job_id, incident_id)
    VALUES ('dispatch_' || NEW.id, NEW.id, NEW.incident_id)
    ON CONFLICT (job_id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS workflow_jobs_dispatch_outbox ON workflow_jobs;
CREATE TRIGGER workflow_jobs_dispatch_outbox
AFTER INSERT ON workflow_jobs
FOR EACH ROW EXECUTE FUNCTION aegis_create_dispatch_outbox();

-- Jobs committed before this migration still need one recovery record.
INSERT INTO workflow_dispatch_outbox (id, job_id, incident_id)
SELECT 'dispatch_' || id, id, incident_id
  FROM workflow_jobs
 WHERE status IN ('queued', 'running')
ON CONFLICT (job_id) DO NOTHING;
