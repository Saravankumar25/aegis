"""Record which embedding model built a runbook's index (ESD §20).

Incremental indexing originally skipped re-embedding whenever the content hash was unchanged.
That is wrong: content is only *one* of the inputs to an index. The index also goes stale when
the embedding model changes, when the dimension changes, or when the chunk table is rebuilt by
a migration — and in every one of those cases the content hash still matches, so ingestion
reported "unchanged" while leaving the corpus unretrievable.

This was not hypothetical. Immediately after `0005` introduced chunking, re-running ingestion
logged `changed=false` for all four runbooks and produced **zero chunks**, so every RAG query
would have returned nothing while the ingestion output looked healthy.

Storing the model that produced the index makes staleness detectable, so changing
`EMBEDDING_MODEL` re-indexes automatically instead of silently serving vectors from a model
that is no longer in use.

Revision ID: 0006_runbook_index_provenance
Revises: 0005_runbook_chunks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_runbook_index_provenance"
down_revision: str | None = "0005_runbook_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, and deliberately left NULL for existing rows: they were indexed by a model we
    # can no longer identify, so they must be treated as stale and re-indexed rather than
    # assumed current.
    op.add_column("runbooks", sa.Column("embedding_model", sa.String(128), nullable=True))
    op.add_column("runbooks", sa.Column("embedding_dim", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("runbooks", "embedding_dim")
    op.drop_column("runbooks", "embedding_model")
