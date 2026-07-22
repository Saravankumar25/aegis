"""Real judge-model wiring for RAGAS (ESD §22).

`ragas_metrics.evaluate_generation` deliberately takes `judge_llm` as a parameter rather than
constructing one itself — an evaluator that defaults its own judge can incur judge-model cost
just by being imported. This module is the one place that default gets built, and only when a
caller asks for it.

**Gemini, not a new provider.** RAGAS's current (non-deprecated) integration point,
`ragas.llms.llm_factory`, wants an OpenAI-compatible client rather than a LangChain model.
Building a second bespoke adapter around `providers.gemini.GeminiProvider` would duplicate the
key-rotation and retry logic that already lives there for no benefit, so this instead points
an `openai.OpenAI` client at Gemini's own OpenAI-compatible endpoint
(`/v1beta/openai/chat/completions`, verified against the real API) using the same keys
`GEMINI_API_KEYS` already configures. One vendor, one credential set, two integration paths.

Cost note: this reuses application credentials for judge calls. It is on-demand only
(`python -m evaluation.ragas_metrics`), never per-commit, for exactly the reason
`ragas_metrics` documents — judge tokens are not free, and a CI gate that spends them on
every push is not a gate anyone can afford to run.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.config import get_settings


class JudgeUnavailable(RuntimeError):
    """No usable judge model could be built — e.g. no Gemini key configured."""


class _RotatingKeyTransport:
    """An httpx transport that rotates Gemini keys on a 429, in place.

    Free-tier Gemini caps requests **per key** at 15/minute/model — trivial for a single
    incident, easy to blow through for an evaluation run that judges several metrics across
    several cases, each costing its own call. `providers.gemini.KeyPool` already solves this
    for the app's own provider, but RAGAS drives the judge through a plain `openai.OpenAI`
    client, which carries exactly one credential. Rather than build a second key-management
    stack, this rewrites the `x-goog-api-key` header per request and retries on the same
    transport, so ragas's own retry logic (which reruns the *call*, not the *transport*)
    keeps working unmodified — it just lands on a different key.

    A transport-level concern, not a provider concern: this exists only to keep one
    on-demand evaluation script from being flaky over free-tier limits, not as a second
    implementation of the app's real rate-limit handling.
    """

    def __init__(self, keys: list[str], base_transport: Any) -> None:
        self._keys = keys
        self._cursor = 0
        self._base = base_transport

    def handle_request(self, request: Any) -> Any:
        last_response = None
        for offset in range(len(self._keys)):
            key = self._keys[(self._cursor + offset) % len(self._keys)]
            request.headers["x-goog-api-key"] = key
            # The OpenAI SDK also sets an Authorization header from the client's api_key;
            # Gemini's OpenAI-compat surface accepts either, but leaving a stale Bearer
            # value from a *different* key would be a confusing thing to leave in place.
            request.headers["Authorization"] = f"Bearer {key}"
            response = self._base.handle_request(request)
            if response.status_code != 429:
                self._cursor = (self._cursor + offset) % len(self._keys)
                return response
            response.read()  # drain before retrying, or httpx holds the connection open
            last_response = response
        return last_response


def build_judge_llm() -> Any:
    """An OpenAI-compatible client wrapped for RAGAS, pointed at Gemini.

    Raises :class:`JudgeUnavailable` rather than returning ``None`` on failure, so a caller
    cannot mistake "judge unavailable" for "judge scored everything at its default".
    """
    settings = get_settings()
    keys = settings.gemini_key_list
    if not keys:
        raise JudgeUnavailable(
            "no GEMINI_API_KEYS configured; the judge model needs at least one real key"
        )
    try:
        import httpx
        from openai import OpenAI
        from ragas.llms import llm_factory
    except ImportError as exc:
        raise JudgeUnavailable(f"ragas/openai not installed: {exc}") from exc

    # Gemini exposes an OpenAI-compatible surface at .../v1beta/openai/ alongside its native
    # one at .../v1beta/. Built from the configured base rather than hardcoded, so pointing
    # `GEMINI_BASE_URL` at a different endpoint (a proxy, a regional endpoint) still resolves
    # correctly.
    native_root = settings.gemini_base_url.rstrip("/").removesuffix("/v1beta")
    http_client = httpx.Client(
        transport=_RotatingKeyTransport(keys, httpx.HTTPTransport()), timeout=60.0
    )
    client = OpenAI(
        api_key=keys[0],  # required by the SDK constructor; overwritten per-request above
        base_url=f"{native_root}/v1beta/openai/",
        http_client=http_client,
    )
    # The RCA-shaped default model, not the fastest one: the judge is deciding whether a
    # claim is *entailed* by evidence, which is closer to RCA's job than to triage's.
    return llm_factory(settings.gemini_model_rca, client=client)


def build_judge_embeddings() -> Any:
    """Embeddings for RAGAS metrics that need a similarity space (``AnswerRelevancy``).

    Reuses the app's own BGE embedder (`rag.embedding.get_embedder`) rather than introducing
    a second embedding stack. `AnswerRelevancy` needs *an* embedding space to compare a
    question regenerated from the answer against the original — it does not need to be the
    same space retrieval uses, but reusing it means no extra model download and no extra
    dependency.

    RAGAS calls these synchronously from threads it manages, so each call opens a
    short-lived event loop rather than assuming one is already running.
    """
    from ragas.embeddings import LangchainEmbeddingsWrapper

    from rag.embedding import get_embedder

    class _SyncBgeEmbeddings:
        """LangChain-shaped (`embed_documents`/`embed_query`) over the app's async embedder.

        LangChain-shaped rather than the newer `BaseRagasEmbedding` because `evaluate()` in
        ragas 0.4.3 pairs with the legacy metric classes, which expect the legacy embeddings
        wrapper. Kept consistent with the metric API rather than mixing generations.
        """

        def __init__(self) -> None:
            self._embedder = get_embedder()

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            return asyncio.run(self._embedder.embed_passages(texts))

        def embed_query(self, text: str) -> list[float]:
            return asyncio.run(self._embedder.embed_query(text))

    return LangchainEmbeddingsWrapper(_SyncBgeEmbeddings())
