"""Unit tests: hashing embedder — deterministic, normalized, semantically ordered."""

from __future__ import annotations

import math

from rag.embedding import DIM, HashingEmbedder


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_deterministic_and_normalized():
    e = HashingEmbedder()
    v1, v2 = e.embed("OOMKilled pod restart memory"), e.embed("OOMKilled pod restart memory")
    assert v1 == v2
    assert len(v1) == DIM
    assert math.isclose(sum(x * x for x in v1), 1.0, rel_tol=1e-9)


def test_related_text_scores_higher_than_unrelated():
    e = HashingEmbedder()
    query = e.embed("pod OOMKilled crashloop memory limit")
    oom_doc = e.embed("Runbook OOMKilled CrashLoopBackOff pods memory limit restarts")
    deploy_doc = e.embed("Runbook elevated 5xx error rate right after a deploy rollback")
    assert _cos(query, oom_doc) > _cos(query, deploy_doc)


def test_empty_text_is_zero_vector():
    assert HashingEmbedder().embed("") == [0.0] * DIM
