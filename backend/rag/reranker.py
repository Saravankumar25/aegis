"""Cross-encoder reranking (ESD §20).

Retrieval and reranking answer different questions. A bi-encoder embeds the query and the
passage *independently*, so it can only compare two summaries produced without knowledge of
each other — cheap enough to run over the whole corpus, but blunt. A cross-encoder reads the
query and passage **together** and scores their actual relationship, which is far more accurate
and far too expensive to run over anything but a shortlist.

So the pipeline over-fetches with the cheap retriever and reorders the shortlist with the
expensive one. This is also why ``rag_candidate_k`` must exceed ``rag_top_k``: a reranker can
only reorder what retrieval handed it, and cannot rescue a correct passage that never made the
candidate pool.

Reranking **degrades rather than fails**. If the model cannot load — no cache, no network on
first run, a bad model name — retrieval returns its fused order instead. Fused order is a
genuinely useful ranking; refusing to answer an incident query because a reordering model is
missing would trade a real capability for a marginal one.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

import anyio

from core.config import get_settings
from core.logging import get_logger

_log = get_logger(component="reranker")


class CrossEncoderReranker:
    """Lazily-loaded ONNX cross-encoder."""

    def __init__(self, model_name: str, cache_dir: str) -> None:
        self.name = model_name
        self._cache_dir = cache_dir
        self._model = None
        self._unavailable = False
        self._lock = threading.Lock()

    def _get_model(self):
        if self._model is not None or self._unavailable:
            return self._model
        with self._lock:
            if self._model is not None or self._unavailable:
                return self._model
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                cache_path = Path(self._cache_dir)
                cache_path.mkdir(parents=True, exist_ok=True)
                _log.info("reranker_loading", model=self.name)
                self._model = TextCrossEncoder(model_name=self.name, cache_dir=str(cache_path))
                _log.info("reranker_ready", model=self.name)
            except Exception as exc:  # noqa: BLE001 — degrade to fused order, never fail search
                # Latched: without this every subsequent query would retry a load that has
                # already been shown to fail, adding its cost to each one.
                self._unavailable = True
                _log.warning("reranker_unavailable", model=self.name, error=str(exc))
            return self._model

    def _score_blocking(self, query: str, passages: list[str]) -> list[float] | None:
        model = self._get_model()
        if model is None:
            return None
        try:
            return list(model.rerank(query, passages))
        except Exception as exc:  # noqa: BLE001 — a scoring failure must not lose the results
            _log.warning("rerank_failed", error=str(exc))
            return None

    async def score(self, query: str, passages: list[str]) -> list[float] | None:
        """Relevance scores aligned to ``passages``, or None if reranking is unavailable."""
        if not passages:
            return []
        return await anyio.to_thread.run_sync(self._score_blocking, query, passages)


@lru_cache
def get_reranker() -> CrossEncoderReranker:
    settings = get_settings()
    return CrossEncoderReranker(
        model_name=settings.rag_reranker_model, cache_dir=settings.embedding_cache_dir
    )
