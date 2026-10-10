-- Fence dispatch settlement so a timed-out dispatcher cannot overwrite the
-- result of a replacement that reclaimed the same outbox row.

ALTER TABLE workflow_dispatch_outbox
    ADD COLUMN IF NOT EXISTS lease_version BIGINT NOT NULL DEFAULT 0;
