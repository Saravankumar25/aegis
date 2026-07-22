"""Add the `escalated` incident state.

The supervisor has always been able to decide an investigation cannot conclude — it routes
to an `escalate` node, which records an agent step and a `escalated` SSE event. But that
node then delegated to `finalize`, which moves the incident to `hypothesis_formed`. So an
incident that had been handed to a human sat in exactly the same state as one still being
actively investigated, and the only signal distinguishing them was an agent step buried in
the timeline.

That makes the dashboard unable to answer the one question that matters during an outage:
which incidents are waiting on me. Observed repeatedly during end-to-end validation — three
consecutive escalations were indistinguishable from in-flight work at the API level.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block on PostgreSQL when the new
value is used in the same transaction. Alembic wraps migrations in one, so the enum value is
added with an autocommit block. Nothing in this migration writes the new value, so the
ordering constraint is satisfied either way; the explicit autocommit makes that independent
of how the migration is invoked.

Revision ID: 0009_escalated_state
Revises: 0008_investigation_lock
"""

from __future__ import annotations

from alembic import op

revision: str = "0009_escalated_state"
down_revision: str | None = "0008_investigation_lock"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE incident_state ADD VALUE IF NOT EXISTS 'escalated'")


def downgrade() -> None:
    # PostgreSQL cannot drop a value from an enum. Reversing this means rebuilding the type
    # and rewriting every dependent column, which would rewrite incident history to remove a
    # state those incidents genuinely reached — a worse outcome than an unused enum value.
    # Rows holding 'escalated' would also have to be reassigned to something they never were.
    raise NotImplementedError(
        "irreversible: PostgreSQL cannot remove an enum value, and rows recorded as "
        "'escalated' have no truthful alternative state to be rewritten to"
    )
