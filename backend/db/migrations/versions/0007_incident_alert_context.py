"""Persist the alert's kind and observed value on the incident (FR-1.3).

`AlertIn` has always carried `kind` ("error_rate", "pod_crash", "latency", ...) and `value`,
and ingestion used `kind` to compute the provisional severity — but neither was ever stored.
The consequence was invisible and real: the investigation graph had no way to recover the
alert kind, so its triage node called `classify_severity(service, "error_rate")` with the kind
**hardcoded**. Every incident was triaged as if it were an error-rate alert, whatever had
actually fired.

Storing them makes the alert's own context available to the Triage agent, which is the
difference between judging an alert and judging its service name.

Revision ID: 0007_incident_alert_context
Revises: 0006_runbook_index_provenance
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0007_incident_alert_context"
down_revision: str | None = "0006_runbook_index_provenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with a default rather than backfilled: existing rows genuinely have no
    # recorded kind, and inventing "error_rate" for them would bake the very bug this
    # migration fixes into the historical data.
    op.add_column("incidents", sa.Column("alert_kind", sa.String(32), nullable=True))
    op.add_column("incidents", sa.Column("alert_value", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("incidents", "alert_value")
    op.drop_column("incidents", "alert_kind")
