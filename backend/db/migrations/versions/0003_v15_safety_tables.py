"""V1.5 safety tables: remediation, leases, breaker, approvals, memory, system flags.

Revision ID: 0003_v15_safety
Revises: 0002_refresh_sessions

The resource_leases partial unique index is the load-bearing piece: one active lease per
target, enforced by Postgres itself (ESD §6).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ENUM, JSONB

revision: str = "0003_v15_safety"
down_revision: str | None = "0002_refresh_sessions"
branch_labels = None
depends_on = None

# create_type=False: the types are created once, explicitly, in upgrade() — the inline
# column references must not try to CREATE TYPE a second time.
remediation_status = ENUM(
    "proposed",
    "leased",
    "approved",
    "executed",
    "rejected",
    "failed",
    "rolled_back",
    name="remediation_status",
    create_type=False,
)
approval_decision = ENUM("approved", "rejected", name="approval_decision", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    remediation_status.create(bind, checkfirst=True)
    approval_decision.create(bind, checkfirst=True)

    op.create_table(
        "remediation_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "incident_id",
            sa.Uuid(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("target_resource_type", sa.String(32), nullable=False),
        sa.Column("target_resource_id", sa.String(256), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("parameters", JSONB(), nullable=False),
        sa.Column("compensating_action", JSONB(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("blast_radius", JSONB(), nullable=False),
        sa.Column("status", remediation_status, nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shadow", sa.Boolean(), nullable=False),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_remediation_actions_incident_id", "remediation_actions", ["incident_id"])

    op.create_table(
        "resource_leases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("target_resource_type", sa.String(32), nullable=False),
        sa.Column("target_resource_id", sa.String(256), nullable=False),
        sa.Column(
            "held_by_remediation_action_id",
            sa.Uuid(),
            sa.ForeignKey("remediation_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_resource_leases_active",
        "resource_leases",
        ["target_resource_type", "target_resource_id"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )

    op.create_table(
        "action_circuit_breaker_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tier1_execution_count", sa.Integer(), nullable=False),
        sa.Column("tripped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "approvals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "remediation_action_id",
            sa.Uuid(),
            sa.ForeignKey("remediation_actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("approver_user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decision", approval_decision, nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_approvals_remediation_action_id", "approvals", ["remediation_action_id"])

    op.create_table(
        "memory_summaries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "incident_id",
            sa.Uuid(),
            sa.ForeignKey("incidents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("service_name", sa.String(128), nullable=False),
        sa.Column("incident_type", sa.String(64), nullable=False),
        sa.Column("symptom", sa.Text(), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("fix", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_memory_summaries_scope", "memory_summaries", ["service_name", "incident_type"]
    )

    op.create_table(
        "system_flags",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "system_flags",
        "memory_summaries",
        "approvals",
        "action_circuit_breaker_events",
        "resource_leases",
        "remediation_actions",
    ):
        op.drop_table(table)
    approval_decision.drop(op.get_bind(), checkfirst=True)
    remediation_status.drop(op.get_bind(), checkfirst=True)
