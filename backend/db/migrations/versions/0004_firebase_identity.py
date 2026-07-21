"""Federated identity columns on users; password becomes optional (ESD §8).

Aegis no longer authenticates with a local password: identity comes from Firebase/Google and
Aegis issues its own session. A federated user therefore has no password at all, so
``hashed_password`` becomes nullable rather than being filled with a sentinel — a sentinel
would be indistinguishable from a real hash to every reader of the column (CLAUDE.md §15:
never silently change a column's meaning).

``firebase_uid`` is the stable subject claim. Email is retained as the human-facing identifier
and the key the role allowlist matches on, but the uid is what survives an email change.

Revision ID: 0004_firebase_identity
Revises: 0003_v15_safety
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_firebase_identity"
down_revision: str | None = "0003_v15_safety"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "hashed_password", existing_type=sa.String(), nullable=True)
    op.add_column("users", sa.Column("firebase_uid", sa.String(128), nullable=True))
    op.add_column("users", sa.Column("display_name", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("photo_url", sa.String(1024), nullable=True))
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    # Unique but nullable: legacy password users have no uid, and Postgres treats each NULL
    # as distinct, so the constraint binds only on rows that actually carry a uid.
    op.create_index("ix_users_firebase_uid", "users", ["firebase_uid"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_firebase_uid", table_name="users")
    op.drop_column("users", "last_login_at")
    op.drop_column("users", "photo_url")
    op.drop_column("users", "display_name")
    op.drop_column("users", "firebase_uid")
    # Federated users have no password to restore. Rather than inventing a hash that would
    # look valid, drop those rows: they cannot authenticate under the old scheme anyway.
    op.execute("DELETE FROM users WHERE hashed_password IS NULL")
    op.alter_column("users", "hashed_password", existing_type=sa.String(), nullable=False)
