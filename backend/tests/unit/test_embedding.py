"""Unit tests: semantic embedding (ESD §20).

The property that justifies replacing the hashing embedder is **semantic** matching — ranking
a passage above an unrelated one when they share no literal tokens. The old embedder scored
near zero on any such pair, so these assertions could not have passed before.

The model is real and loaded from the local cache; there is no fake embedder anywhere in the
runtime or here (CLAUDE.md §18).
"""

from __future__ import annotations

import math

import pytest

from rag.embedding import get_embedder

pytestmark = pytest.mark.embedding


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_dimension_matches_configuration():
    embedder = get_embedder()
    [vector] = await embedder.embed_passages(["checkout-service is returning 503s"])
    assert len(vector) == embedder.dim


async def test_vectors_are_normalized():
    [vector] = await get_embedder().embed_passages(["pods are restarting"])
    assert math.isclose(sum(x * x for x in vector), 1.0, rel_tol=1e-4)


async def test_deterministic():
    embedder = get_embedder()
    first = await embedder.embed_passages(["OOMKilled pod restart memory"])
    second = await embedder.embed_passages(["OOMKilled pod restart memory"])
    assert first[0] == pytest.approx(second[0], rel=1e-6)


async def test_semantic_match_without_shared_vocabulary():
    """The whole point of the change: no lexical overlap, correct ranking anyway.

    "ran out of memory" shares no token with "OOMKilled ... CrashLoopBackOff", so a
    bag-of-words embedder ranks it no higher than an unrelated passage.
    """
    embedder = get_embedder()
    query = await embedder.embed_query("the container ran out of memory and keeps restarting")
    oom, deploy = await embedder.embed_passages(
        [
            "Pods enter CrashLoopBackOff after being OOMKilled; raise the memory limit.",
            "Elevated 5xx immediately following a deploy; roll back the release.",
        ]
    )
    assert _cos(query, oom) > _cos(query, deploy)


async def test_batching_preserves_order():
    """A batch must return vectors aligned to its inputs, or every chunk is mislabelled."""
    embedder = get_embedder()
    texts = ["disk pressure on the node", "certificate expired", "database connection pool"]
    batched = await embedder.embed_passages(texts)
    for text, batched_vector in zip(texts, batched, strict=True):
        [individual] = await embedder.embed_passages([text])
        assert batched_vector == pytest.approx(individual, rel=1e-6)


async def test_empty_batch_returns_empty():
    assert await get_embedder().embed_passages([]) == []
