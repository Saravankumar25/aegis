"""User model for auth and RBAC (ESD §6, §8).

Identity is federated through Firebase/Google: ``firebase_uid`` is the stable subject claim
and ``hashed_password`` is therefore ``None`` for every user created after the migration to
federated auth. Role drives server-side authorization on every state-changing endpoint
(ESD §8) and is derived from the operator-controlled allowlist, never from a client claim.
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from db.enums import UserRole
from db.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An authenticated user with an RBAC role."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    # Stable Firebase subject claim; survives an email change. Unique but nullable so
    # pre-federation rows (which have no uid) do not collide with each other.
    firebase_uid: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True, index=True
    )
    # Null for every federated user — they authenticate against Google, not against us.
    hashed_password: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), nullable=False, default=UserRole.viewer
    )
