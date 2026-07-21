"""Embedding Strategy for the runbook corpus (ESD §20: open-source, no per-call cost).

Runs **BGE locally through ONNX** (`fastembed`) rather than a hosted embedding API or a
PyTorch stack. The reasoning, in the terms that actually decided it:

* *Retrieval quality* — BGE is a genuine semantic model. The embedder it replaces was a hashed
  bag-of-words, which scores ~0 for a query sharing no literal tokens with the document; the
  operational corpus is full of exactly that mismatch ("out of memory" vs `OOMKilled`).
* *Deployment* — ONNX Runtime pulls ~90MB of dependencies against ~2.5GB for torch, which is
  the difference between a reasonable container image and an unreasonable one.
* *Offline capability* — the model is fetched once and cached on disk; every call after that is
  local. Nothing here reaches the network at request time, so CLAUDE.md §18 (local-first, no
  hard dependency on a paid cloud service) holds.
* *Cost and scalability* — zero per-call cost, batching built in, and swapping model or
  dimension is configuration rather than code.

**Asymmetric encoding matters.** BGE is trained with an instruction prefix on the *query* side
only. `fastembed` applies it via `query_embed`, so queries and passages go through different
entry points here deliberately — using one for both measurably degrades retrieval.

Inference is CPU-bound and synchronous, so every public entry point is async and dispatches to
a worker thread: embedding a batch inline would stall the event loop for every concurrent
incident (CLAUDE.md §3).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

import anyio

from core.config import get_settings
from core.logging import get_logger

_log = get_logger(component="embedding")


class Embedder(ABC):
    """Embedding strategy: text → a fixed-width L2-normalized vector."""

    name: str = "base"
    dim: int = 0

    @abstractmethod
    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        """Embed documents for storage."""

    @abstractmethod
    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query (instruction-prefixed for BGE-family models)."""


class FastEmbedEmbedder(Embedder):
    """Local ONNX BGE embedder.

    The model is loaded lazily and once. Loading costs seconds and tens of megabytes of RAM,
    so doing it at import time would penalise every process that never embeds anything —
    including the API, which only embeds on a search request.
    """

    def __init__(self, model_name: str, dim: int, cache_dir: str, batch_size: int) -> None:
        self.name = model_name
        self.dim = dim
        self._cache_dir = cache_dir
        self._batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()

    def _get_model(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:  # another thread won the race
                return self._model
            from fastembed import TextEmbedding

            cache_path = Path(self._cache_dir)
            cache_path.mkdir(parents=True, exist_ok=True)
            _log.info("embedding_model_loading", model=self.name, cache_dir=str(cache_path))
            model = TextEmbedding(model_name=self.name, cache_dir=str(cache_path))

            # Fail loudly at load time if the model's width disagrees with configuration.
            # Discovering this at INSERT time instead would surface as an opaque pgvector
            # dimension error with no indication of which knob is wrong.
            probe = len(next(iter(model.embed(["dimension probe"]))))
            if probe != self.dim:
                raise RuntimeError(
                    f"embedding model {self.name} produces {probe}-dim vectors but "
                    f"EMBEDDING_DIM is {self.dim}. Update EMBEDDING_DIM and migrate the "
                    f"pgvector columns to match — they cannot differ."
                )
            self._model = model
            _log.info("embedding_model_ready", model=self.name, dim=self.dim)
            return model

    def _embed_passages_blocking(self, texts: list[str]) -> list[list[float]]:
        model = self._get_model()
        return [v.tolist() for v in model.embed(texts, batch_size=self._batch_size)]

    def _embed_query_blocking(self, text: str) -> list[float]:
        model = self._get_model()
        # query_embed applies BGE's retrieval instruction prefix; embed() does not.
        return next(iter(model.query_embed([text]))).tolist()

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await anyio.to_thread.run_sync(self._embed_passages_blocking, texts)

    async def embed_query(self, text: str) -> list[float]:
        return await anyio.to_thread.run_sync(self._embed_query_blocking, text)


@lru_cache
def get_embedder() -> Embedder:
    """The configured embedder (process-wide; the model is loaded at most once)."""
    settings = get_settings()
    return FastEmbedEmbedder(
        model_name=settings.embedding_model,
        dim=settings.embedding_dim,
        cache_dir=settings.embedding_cache_dir,
        batch_size=settings.embedding_batch_size,
    )
