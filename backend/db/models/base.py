"""Declarative base and shared column mixins for all ORM models (ESD §6).

Table and column names are ``snake_case`` per CLAUDE.md §4. Primary keys are UUIDs generated
application-side so an id is known before the row is flushed.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Root of the ORM model hierarchy; owns the shared metadata Alembic reflects."""


class UUIDPrimaryKeyMixin:
    """Adds a UUID primary key with an application-side default."""

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """Adds a server-defaulted ``created_at`` timestamp (timezone-aware)."""

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
