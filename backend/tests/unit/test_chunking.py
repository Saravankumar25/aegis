"""Unit tests: document chunking (ESD §20).

Chunking is pure string logic, so it is tested with no model, no database, and no I/O — the
whole reason it lives in its own module.
"""

from __future__ import annotations

import pytest

from rag.chunking import ChunkConfig, chunk_markdown

RUNBOOK = """# OOM CrashLoop

Pods are killed by the kernel when they exceed their memory limit.

## Detection

Look for `OOMKilled` in the pod's last state and repeated restarts.

## Mitigation

Raise `resources.limits.memory` and redeploy.

### Rollback

If the raise does not help, scale the deployment back down.
"""


def test_splits_on_headings():
    chunks = chunk_markdown(RUNBOOK)
    assert len(chunks) > 1
    paths = [c.heading_path for c in chunks]
    assert any("Detection" in p for p in paths)
    assert any("Mitigation" in p for p in paths)


def test_heading_path_reflects_nesting():
    """A nested section must carry its parents, or a fragment loses its context."""
    chunks = chunk_markdown(RUNBOOK)
    rollback = next(c for c in chunks if "Rollback" in c.heading_path)
    assert "OOM CrashLoop" in rollback.heading_path
    assert "Mitigation" in rollback.heading_path


def test_sibling_heading_does_not_accumulate():
    """Popping to the right level matters: Detection must not appear under Mitigation."""
    chunks = chunk_markdown(RUNBOOK)
    mitigation = next(
        c
        for c in chunks
        if c.heading_path.endswith("Mitigation") and "Rollback" not in c.heading_path
    )
    assert "Detection" not in mitigation.heading_path


def test_embedding_text_includes_heading_context():
    chunks = chunk_markdown(RUNBOOK)
    detection = next(c for c in chunks if "Detection" in c.heading_path)
    assert "Detection" in detection.embedding_text
    assert "OOMKilled" in detection.embedding_text


def test_indices_are_contiguous_from_zero():
    chunks = chunk_markdown(RUNBOOK)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_oversized_section_is_split_with_overlap():
    config = ChunkConfig(max_chars=200, overlap_chars=50, min_chars=10)
    body = "\n\n".join(f"Paragraph {i} with some operational detail." for i in range(20))
    chunks = chunk_markdown(f"# Big\n\n{body}", config)
    assert len(chunks) > 1
    assert all(len(c.content) <= config.max_chars for c in chunks)


def test_single_paragraph_larger_than_budget_still_splits():
    """A log dump with no paragraph breaks must not produce one oversized chunk."""
    config = ChunkConfig(max_chars=100, overlap_chars=20, min_chars=10)
    chunks = chunk_markdown("# Dump\n\n" + ("x" * 500), config)
    assert len(chunks) > 1
    assert all(len(c.content) <= config.max_chars for c in chunks)


def test_document_without_headings_is_still_chunked():
    """Retrievability cannot depend on a document being well-formatted."""
    chunks = chunk_markdown("just a bare note about checkout latency with no headings at all")
    assert len(chunks) == 1
    assert chunks[0].heading_path == ""


def test_empty_document_yields_nothing():
    assert chunk_markdown("   \n\n  ") == []


def test_overlap_must_be_smaller_than_max():
    """Guards a config that would never advance and would loop forever."""
    with pytest.raises(ValueError):
        ChunkConfig(max_chars=100, overlap_chars=100)
