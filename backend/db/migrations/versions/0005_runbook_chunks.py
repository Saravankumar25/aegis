"""Chunk-level RAG index: runbook_chunks with HNSW + GIN (ESD §6, §20).

Retrieval moves from whole documents to passages. Two consequences are encoded here:

1. ``runbooks.embedding`` is **dropped**, not left in place. It is a 768-dim vector produced by
   the removed hashing embedder; the new model emits 384 dims, so the column can neither be
   reused nor reinterpreted. Leaving a stale vector that no code path writes would be exactly
   the "column whose meaning silently changed" that CLAUDE.md §15 forbids — and worse, it would
   still answer similarity queries, with numbers that mean nothing.
2. Chunk text is indexed twice: pgvector/HNSW for semantic match, tsvector/GIN for lexical
   match on identifiers (`OOMKilled`, `checkout-service`) that embeddings handle poorly.

``search_vector`` is maintained by a trigger rather than by the application. Application-side
maintenance drifts the moment any other writer touches the table, and the lexical index silently
returning stale rows is far harder to notice than a failed write.

Revision ID: 0005_runbook_chunks
Revises: 0004_firebase_identity
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from core.config import get_settings

revision: str = "0005_runbook_chunks"
down_revision: str | None = "0004_firebase_identity"
branch_labels = None
depends_on = None

EMBEDDING_DIM = get_settings().embedding_dim


def upgrade() -> None:
    op.create_table(
        "runbook_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "runbook_id",
            sa.Uuid(),
            sa.ForeignKey("runbooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("heading_path", sa.String(512), nullable=False, server_default=""),
        sa.Column(
            "service_tags",
            postgresql.ARRAY(sa.String()),
            nullable=False,
            server_default=sa.text("'{}'::varchar[]"),
        ),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
    )
    op.create_index("ix_runbook_chunks_runbook_id", "runbook_chunks", ["runbook_id"])
    op.create_index(
        "ix_runbook_chunks_runbook_chunk",
        "runbook_chunks",
        ["runbook_id", "chunk_index"],
        unique=True,
    )
    op.execute(
        "CREATE INDEX ix_runbook_chunks_embedding_hnsw ON runbook_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    # GIN over the tsvector for lexical retrieval, and over service_tags so a metadata filter
    # narrows the scan rather than being applied after it.
    op.execute(
        "CREATE INDEX ix_runbook_chunks_search_vector ON runbook_chunks USING gin (search_vector)"
    )
    op.execute(
        "CREATE INDEX ix_runbook_chunks_service_tags ON runbook_chunks USING gin (service_tags)"
    )

    op.execute(
        """
        CREATE FUNCTION runbook_chunks_search_vector_update() RETURNS trigger AS $$
        BEGIN
          NEW.search_vector :=
            setweight(to_tsvector('english', coalesce(NEW.heading_path, '')), 'A') ||
            setweight(to_tsvector('english', coalesce(NEW.content, '')), 'B');
          RETURN NEW;
        END
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_runbook_chunks_search_vector "
        "BEFORE INSERT OR UPDATE OF content, heading_path ON runbook_chunks "
        "FOR EACH ROW EXECUTE FUNCTION runbook_chunks_search_vector_update()"
    )

    # The document-level vector is unusable at the new dimension — see the module docstring.
    op.execute("DROP INDEX IF EXISTS ix_runbooks_embedding_hnsw")
    op.drop_column("runbooks", "embedding")


def downgrade() -> None:
    op.add_column("runbooks", sa.Column("embedding", Vector(768), nullable=True))
    op.execute(
        "CREATE INDEX ix_runbooks_embedding_hnsw ON runbooks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_runbook_chunks_search_vector ON runbook_chunks")
    op.execute("DROP FUNCTION IF EXISTS runbook_chunks_search_vector_update()")
    op.drop_table("runbook_chunks")
