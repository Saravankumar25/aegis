"""User model for auth and RBAC (ESD §6, §8).

Passwords are stored only as a hash (never plaintext, never logged — CLAUDE.md §12). Role drives
server-side authorization on every state-changing endpoint (ESD §8).
"""

from __future__ import annotations

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from db.enums import UserRole
from db.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """An authenticated user with an RBAC role."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), nullable=False, default=UserRole.viewer
    )
