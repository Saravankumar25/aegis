"""Runbook / postmortem corpus model for RAG grounding (ESD §6, PRD FR-3.3).

The **document of record**. It carries no embedding of its own: retrieval operates on
``RunbookChunk`` rows, because a single vector for a whole document averages away the
distinction between the sections it covers and yields citations too coarse to act on.

``content_hash`` versions the document — it drives incremental re-indexing (unchanged content
is never re-embedded) and lets the RCA Agent detect a citation that has gone stale because the
underlying document changed since it was cited (ESD §6 [review fix]).
"""

from __future__ import annotations

import datetime

from sqlalchemy import DateTime, Enum, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from db.enums import RunbookSource
from db.models.base import Base, UUIDPrimaryKeyMixin


class Runbook(UUIDPrimaryKeyMixin, Base):
    """A runbook or past-incident postmortem available for retrieval grounding."""

    __tablename__ = "runbooks"

    title: Mapped[str] = mapped_column(String, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False, index=True)
    source: Mapped[RunbookSource] = mapped_column(
        Enum(RunbookSource, name="runbook_source"), nullable=False
    )
    source_company: Mapped[str | None] = mapped_column(String, nullable=True)
    service_tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    # Index provenance: which embedding model/dimension produced this document's chunks.
    # Content is only one input to an index — a model or dimension change invalidates it just
    # as surely as an edit does, and without these the staleness is undetectable.
    embedding_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ingested_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
