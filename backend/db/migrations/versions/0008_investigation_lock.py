"""Ownership columns so two workers cannot investigate one incident concurrently.

The claim in `worker.run_investigation` already took a row lock, which looked sufficient
and was not. Worker A locks the row, transitions `open -> investigating`, **commits**
(releasing the lock), and only then runs the graph — which takes a minute of LLM calls.
Worker B arrives during that minute, takes the now-free lock, and finds the state is
`investigating`. Its guard only returns for states *past* investigation, so B proceeds to
investigate the same incident. Observed live: one incident produced two complete sets of
agent steps, doubling LLM spend and interleaving two independent hypotheses under one id.

`investigating` has to stay claimable — the ESD §4 reconciliation sweep re-runs incidents
stranded by a crashed worker, and that is the state they are stranded in. So the fix is not
to forbid claiming it but to distinguish *actively owned* from *abandoned*, which needs
ownership recorded rather than inferred from the state alone.

Deliberately columns on `incidents` rather than a row in `resource_leases`: that table's
lease is foreign-keyed to a remediation action and models "at most one action per
infrastructure target", which is a different invariant with a different lifetime. Widening
it to mean two things would weaken the constraint that makes it trustworthy.

Revision ID: 0008_investigation_lock
Revises: 0007_incident_alert_context
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_investigation_lock"
down_revision: str | None = "0007_incident_alert_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable and unbackfilled: existing rows were never owned by a running worker, and a
    # synthetic owner would make an abandoned incident look actively held until its lock
    # aged out.
    op.add_column(
        "incidents",
        sa.Column("investigation_locked_by", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "incidents",
        sa.Column("investigation_locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The claim query filters on `state` plus lock age; this index keeps the reconciliation
    # sweep's scan cheap as the incident table grows.
    op.create_index(
        "ix_incidents_investigation_lock",
        "incidents",
        ["state", "investigation_locked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_incidents_investigation_lock", table_name="incidents")
    op.drop_column("incidents", "investigation_locked_at")
    op.drop_column("incidents", "investigation_locked_by")
