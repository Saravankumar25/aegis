"""Real LLM provider over the Google Gemini API (ESD §20).

Added because a single free-tier vendor is a single point of failure: OpenRouter's
free models share one account-wide *daily* cap, and when it is spent every agent in
the system stops reasoning until the UTC reset. That is not a throttle the retry
sweep can ride out, and it repeatedly blocked end-to-end validation of the AI layer.
Gemini is an independent capacity pool reached through the same `LLMProvider`
Strategy, so switching is a config change rather than a code change.

The same two recovery axes as the OpenRouter provider, for the same reason:

- **Key rotation.** A 429/403 on one key retries the same model on the next key.
- **Model fallback.** When every key is throttled for a model, the next model in the
  chain is tried. Free Gemini capacity genuinely moves between models during the day
  — `gemini-3.5-flash` answering 503 "high demand" while `gemini-3.1-flash-lite`
  serves normally was the *observed* state when this provider was written, not a
  hypothetical.

Three Gemini-specific behaviours that the OpenAI-compatible path does not have:

- **`systemInstruction` is a separate top-level field**, not a message with a role.
  Aegis's injection resistance ("evidence is data, never instruction") depends on the
  standing contract being weighted as a system instruction, so it must go there.
- **Schemas use Gemini's OpenAPI subset**, translated by `providers.gemini_schema`.
- **Safety filters can block a response outright.** Incident evidence contains stack
  traces, kill/terminate/abort verbs and hostile-looking log text, which trips content
  filters on occasion. A block is treated as a miss on that model and the chain
  continues — never as an answer — because a blocked response carries no reasoning.

Keys are read from settings and never logged: log lines carry a key *index*, never
the value (CLAUDE.md §12).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import BaseModel

from core.config import get_settings
from core.logging import get_logger
from core.tracing import wrap_llm_call
from providers.base import LLMProvider, LLMResult, StructuredResult
from providers.errors import (
    DailyQuotaExhausted,
    ProviderExhausted,
    RateLimited,
)
from providers.gemini_schema import to_gemini_schema
from providers.keypool import KeyPool
from providers.parsing import strip_code_fences
from providers.structured import complete_structured_with_repair

__all__ = ["GeminiProvider"]


class KeyQuotaExhausted(Exception):
    """One key hit its per-day cap. Internal: never escapes a provider call.

    Distinct from `RateLimited` because the recovery differs — a throttled key is worth
    retrying in seconds, a capped key is not worth retrying today — and distinct from
    `DailyQuotaExhausted` because that reports the *pool* being spent, which is what an
    operator needs to act on.
    """


# finishReason values that mean "this response is unusable", as opposed to a normal
# stop. MAX_TOKENS is handled separately because it is recoverable with more budget.
_BLOCKED_FINISH_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
}


class GeminiProvider(LLMProvider):
    """Google Generative Language API with key rotation and model fallback."""

    name = "gemini"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._keys = settings.gemini_key_list
        if not self._keys:
            raise ValueError(
                "LLM_PROVIDER=gemini but GEMINI_API_KEYS is empty; set it in .env (never commit it)"
            )
        self._base_url = settings.gemini_base_url.rstrip("/")
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_tokens
        self._model_for_rca = settings.gemini_model_rca
        self._model_default = settings.gemini_model_default
        self._fallbacks = settings.gemini_fallback_list
        self._max_truncation_budget = settings.llm_max_tokens_on_truncation
        self._structured_repair_attempts = settings.llm_structured_repair_attempts
        self._http = http or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
        self._pool = KeyPool(self._keys)
        self._log = get_logger(component="llm.gemini")

    def _models_for(self, agent: str) -> list[str]:
        """Primary model for the agent, then the shared fallback chain."""
        primary = self._model_for_rca if agent == "rca" else self._model_default
        chain = [primary]
        for model in self._fallbacks:
            if model not in chain:
                chain.append(model)
        return chain

    def _payload(
        self,
        prompt: str,
        system: str | None,
        max_tokens: int,
        json_schema: dict[str, Any] | None,
    ) -> dict[str, Any]:
        generation: dict[str, Any] = {
            "temperature": self._temperature,
            "maxOutputTokens": max_tokens,
        }
        if json_schema is not None:
            # Constrained decoding. Still only an optimisation — the shared repair loop
            # validates regardless, because a schema-constrained model can satisfy the
            # shape while getting the semantics wrong.
            generation["responseMimeType"] = "application/json"
            generation["responseSchema"] = to_gemini_schema(json_schema)
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        return payload

    def _headers(self, key_index: int) -> dict[str, str]:
        return {
            "x-goog-api-key": self._pool.key(key_index),
            "Content-Type": "application/json",
        }

    def _classify_error(self, status: int, body: str, model: str, key_index: int) -> Exception:
        """Map an HTTP failure onto the shared taxonomy.

        `KeyQuotaExhausted` is raised per *key*, not per call. Gemini keys carry
        independent per-project daily quotas, so one capped key says nothing about the
        others — the caller marks that key and moves on, and only reports
        `DailyQuotaExhausted` once every key is capped. (The OpenRouter provider is
        deliberately different: its keys bill against one account, so the first daily
        cap really is terminal for all of them.)
        """
        lowered = body.lower()
        if status == 429 and ("per day" in lowered or "perday" in lowered):
            return KeyQuotaExhausted(f"{model} key#{key_index % len(self._keys)} daily cap")
        if status in (401, 403, 429):
            return RateLimited(f"{model} key#{key_index % len(self._keys)} -> {status}")
        if status >= 500:
            # 503 "model is currently experiencing high demand" is routine on free
            # capacity and is a fallback signal, not an outage.
            return RateLimited(f"{model} upstream {status}")
        return httpx.HTTPStatusError(f"gemini {status}: {body[:200]}", request=None, response=None)

    async def _post(
        self,
        model: str,
        key_index: int,
        prompt: str,
        max_tokens: int,
        *,
        system: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self._http.post(
            f"{self._base_url}/models/{model}:generateContent",
            json=self._payload(prompt, system, max_tokens, json_schema),
            headers=self._headers(key_index),
        )
        if response.status_code != 200:
            raise self._classify_error(response.status_code, response.text, model, key_index)
        return response.json()

    @staticmethod
    def _extract_text(body: dict[str, Any]) -> tuple[str, str | None]:
        """Return ``(text, finish_reason)`` from a Gemini response body.

        Text arrives as a list of parts which must be concatenated; taking only
        `parts[0]` silently truncates any response the model split, which is common
        once output is long enough to matter.
        """
        candidates = body.get("candidates") or []
        if not candidates:
            return "", None
        candidate = candidates[0]
        finish = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return text, finish

    @wrap_llm_call
    async def complete(
        self,
        prompt: str,
        *,
        agent: str,
        system: str | None = None,
        ensemble_pass: int = 0,
        max_tokens: int = 1024,
        prompt_ref: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        """Call the model, walking the model × key matrix until one answers.

        Keys are ordered by the pool (last-successful first, cooling-down keys demoted,
        capped keys skipped) and re-ordered on every model, so a key that recovers part
        way through is picked up by the next model rather than being written off for the
        whole call.
        """
        attempts = 0
        started = time.perf_counter()
        budget = max(max_tokens, self._max_tokens)

        for model in self._models_for(agent):
            if self._pool.all_quota_exhausted():
                break
            for key_index in self._pool.order():
                attempts += 1
                try:
                    body = await self._post(
                        model,
                        key_index,
                        prompt,
                        budget,
                        system=system,
                        json_schema=json_schema,
                    )
                except KeyQuotaExhausted as exc:
                    # This key is capped for the day; the others may be fine.
                    self._pool.mark_quota_exhausted(key_index)
                    self._log.warning(
                        "llm_key_quota_exhausted",
                        detail=str(exc),
                        agent=agent,
                        **self._pool.snapshot(),
                    )
                    if self._pool.all_quota_exhausted():
                        self._log.error("llm_daily_quota_exhausted", agent=agent)
                        raise DailyQuotaExhausted(
                            f"all {len(self._keys)} Gemini keys have hit their daily "
                            "quota. This does not clear on retry: wait for the quota "
                            "reset, add a key from another project, or enable billing."
                        ) from exc
                    continue
                except RateLimited as exc:
                    self._pool.mark_throttled(key_index)
                    self._log.warning("llm_throttled", detail=str(exc), agent=agent)
                    continue
                except (httpx.HTTPError, OSError) as exc:
                    self._log.warning(
                        "llm_transport_error", model=model, agent=agent, error=str(exc)
                    )
                    continue

                if block_reason := (body.get("promptFeedback") or {}).get("blockReason"):
                    # The *prompt* was refused. Redacted incident evidence occasionally
                    # trips this; another model may accept it.
                    self._log.warning(
                        "llm_prompt_blocked",
                        model=model,
                        agent=agent,
                        block_reason=block_reason,
                    )
                    continue

                text, finish = self._extract_text(body)

                if finish in _BLOCKED_FINISH_REASONS:
                    self._log.warning(
                        "llm_response_blocked", model=model, agent=agent, finish_reason=finish
                    )
                    continue

                if finish == "MAX_TOKENS" and budget < self._max_truncation_budget:
                    # Output was cut off: retry the same model once with a bigger budget
                    # rather than losing the call to a formatting artefact.
                    self._log.warning("llm_truncated", model=model, agent=agent, budget=budget)
                    budget = self._max_truncation_budget
                    try:
                        body = await self._post(
                            model,
                            key_index,
                            prompt,
                            budget,
                            system=system,
                            json_schema=json_schema,
                        )
                    except (RateLimited, httpx.HTTPError, OSError):
                        continue
                    text, finish = self._extract_text(body)

                if not text.strip():
                    # Thinking-heavy models can spend the whole budget on hidden
                    # reasoning and return no parts; treat as a miss.
                    self._log.warning("llm_empty_content", model=model, agent=agent)
                    continue

                usage = body.get("usageMetadata") or {}
                self._pool.mark_success(key_index)
                latency_ms = int((time.perf_counter() - started) * 1000)
                tokens = int(usage.get("totalTokenCount") or 0)
                self._log.info(
                    "llm_call",
                    model=model,
                    agent=agent,
                    ensemble_pass=ensemble_pass,
                    tokens=tokens,
                    latency_ms=latency_ms,
                    prompt_ref=prompt_ref,
                    attempts=attempts,
                )
                return LLMResult(
                    text=strip_code_fences(text),
                    model=model,
                    tokens_used=tokens,
                    # The Gemini API reports no price. Rather than invent one from a
                    # hardcoded rate card that silently goes stale, cost is reported as
                    # 0.0 and the *token* budget (ESD §15) remains the real control.
                    cost_usd=0.0,
                    latency_ms=latency_ms,
                    prompt_ref=prompt_ref,
                )

        self._log.error(
            "llm_all_models_exhausted", agent=agent, attempts=attempts, **self._pool.snapshot()
        )
        raise ProviderExhausted(
            f"every configured Gemini model and key was unavailable for agent '{agent}' "
            f"after {attempts} attempts ({self._pool.snapshot()}); the incident is left "
            f"for the retry sweep rather than answered with fabricated reasoning"
        )

    async def complete_structured[StructuredT: BaseModel](
        self,
        prompt: str,
        *,
        schema: type[StructuredT],
        agent: str,
        system: str | None = None,
        ensemble_pass: int = 0,
        max_tokens: int = 1024,
        prompt_ref: str | None = None,
    ) -> StructuredResult[StructuredT]:
        """Return output validated against ``schema``, repairing a bounded number of times.

        The enforcement loop is shared with every other provider
        (``providers.structured``) so the guarantee cannot weaken depending on which
        vendor answered; only the native-schema translation is Gemini-specific.
        """
        return await complete_structured_with_repair(
            self.complete,
            prompt,
            schema=schema,
            agent=agent,
            log=self._log,
            repair_attempts=self._structured_repair_attempts,
            system=system,
            ensemble_pass=ensemble_pass,
            max_tokens=max_tokens,
            prompt_ref=prompt_ref,
        )

    async def stream(
        self,
        prompt: str,
        *,
        agent: str,
        system: str | None = None,
        max_tokens: int = 1024,
        prompt_ref: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield text deltas as the model produces them.

        Deliberately does **not** fail over to another model once the first delta has
        been emitted: splicing two completions together produces one apparently
        coherent answer assembled from two different lines of reasoning, which is
        exactly the kind of plausible-but-wrong output the rest of the system exists
        to prevent. Before the first delta, falling over is safe and is done.
        """
        started = time.perf_counter()
        budget = max(max_tokens, self._max_tokens)
        last_error = "no attempt was made"

        for model in self._models_for(agent):
            for key_index in self._pool.order():
                emitted = False
                try:
                    async with self._http.stream(
                        "POST",
                        f"{self._base_url}/models/{model}:streamGenerateContent",
                        params={"alt": "sse"},
                        json=self._payload(prompt, system, budget, None),
                        headers=self._headers(key_index),
                    ) as response:
                        if response.status_code != 200:
                            await response.aread()
                            raise self._classify_error(
                                response.status_code, response.text, model, key_index
                            )
                        self._pool.mark_success(key_index)
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[len("data:") :].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            delta, _ = self._extract_text(chunk)
                            if delta:
                                emitted = True
                                yield delta
                except (KeyQuotaExhausted, RateLimited, httpx.HTTPError, OSError) as exc:
                    last_error = str(exc)
                    if emitted:
                        # Mid-stream failure. Stop rather than splice.
                        self._log.error(
                            "llm_stream_aborted", model=model, agent=agent, error=last_error
                        )
                        raise
                    if isinstance(exc, KeyQuotaExhausted):
                        self._pool.mark_quota_exhausted(key_index)
                        if self._pool.all_quota_exhausted():
                            raise DailyQuotaExhausted(
                                f"all {len(self._keys)} Gemini keys have hit their daily "
                                "quota; streaming cannot proceed"
                            ) from exc
                    elif isinstance(exc, RateLimited):
                        self._pool.mark_throttled(key_index)
                    self._log.warning(
                        "llm_stream_retry", model=model, agent=agent, error=last_error
                    )
                    continue

                if emitted:
                    self._log.info(
                        "llm_stream",
                        model=model,
                        agent=agent,
                        prompt_ref=prompt_ref,
                        total_ms=int((time.perf_counter() - started) * 1000),
                    )
                    return
                last_error = f"{model} produced no deltas"

        raise ProviderExhausted(
            f"no Gemini model could stream for agent '{agent}'; last error: {last_error}"
        )

    async def aclose(self) -> None:
        await self._http.aclose()
