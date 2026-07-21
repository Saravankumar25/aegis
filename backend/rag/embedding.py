"""Embedding Strategy for the runbook corpus (ESD §20: open-source, no per-call cost).

Default is a deterministic 768-dim hashed bag-of-words embedder: dependency-free, instant,
and good enough for keyword-heavy operational text on a 3-service corpus. The interface is
the contract — swapping in BGE (the ESD §20 target) is a provider change, not a schema
change, because both emit 768-dim L2-normalized vectors into the same pgvector column.
"""

from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

DIM = 768
_TOKEN = re.compile(r"[a-z0-9_]{2,}")


class Embedder(ABC):
    """Embedding strategy: text → 768-dim L2-normalized vector."""

    name: str = "base"

    @abstractmethod
    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder(Embedder):
    """Deterministic hashed bag-of-words with sublinear term weighting."""

    name = "hashing-768"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * DIM
        counts: dict[str, int] = {}
        for token in _TOKEN.findall(text.lower()):
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % DIM
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


def get_embedder() -> Embedder:
    """The configured embedder (only the hashing embedder ships in MVP)."""
    return HashingEmbedder()
