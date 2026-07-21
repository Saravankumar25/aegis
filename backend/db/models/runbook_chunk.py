"""Retrievable runbook chunks — the unit RAG actually searches (ESD §6, PRD FR-3.3).

A `Runbook` is the document of record; a `RunbookChunk` is a passage of it with its own
embedding. Retrieval targets chunks rather than documents because a whole-document embedding
averages away the distinction between the sections it contains, and because a citation to a
200-line document does not tell a responder where to look.

Two indexed representations coexist deliberately:

* ``embedding`` (pgvector, HNSW) — semantic similarity, which catches "out of memory" matching
  ``OOMKilled``.
* ``search_vector`` (tsvector, GIN) — lexical match, which catches the exact identifiers
  semantic models are weakest on: error codes, flag names, service names.

Neither subsumes the other, which is why retrieval fuses both (see ``rag.store``).
"""

from __future__ import annotations

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from core.config import get_settings
from db.models.base import Base, UUIDPrimaryKeyMixin

# Read once at import: the pgvector column width is fixed at migration time, so this must
# agree with the deployed schema. `FastEmbedEmbedder` re-checks it against the live model.
EMBEDDING_DIM = get_settings().embedding_dim


class RunbookChunk(UUIDPrimaryKeyMixin, Base):
    """One embedded passage of a runbook."""

    __tablename__ = "runbook_chunks"

    runbook_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runbooks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Heading trail ("Mitigation › Rollback"), stored so a citation can name the section a
    # passage came from rather than only the document.
    heading_path: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    # Denormalized from the parent runbook so metadata filtering happens in the same index
    # scan as retrieval, instead of forcing a join before the ANN search can be narrowed.
    service_tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)

    __table_args__ = (
        Index("ix_runbook_chunks_runbook_chunk", "runbook_id", "chunk_index", unique=True),
    )
