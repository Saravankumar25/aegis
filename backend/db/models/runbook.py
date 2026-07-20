"""Runbook / postmortem corpus model for RAG grounding (ESD §6, PRD FR-3.3).

Documents are embedded with an open-source 768-dim model (BGE, ESD §20) into a pgvector column with
an HNSW index for sub-second similarity search (ESD §15). ``content_hash`` lets the RCA Agent detect
a stale citation if the underlying document changed since it was embedded (ESD §6 [review fix]).
"""

from __future__ import annotations

import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from db.enums import RunbookSource
from db.models.base import Base, UUIDPrimaryKeyMixin

EMBEDDING_DIM = 768


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
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    ingested_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
