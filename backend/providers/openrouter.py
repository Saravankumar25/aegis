"""Real LLM provider over OpenRouter's OpenAI-compatible API (ESD §20).

Free-tier models are the target (PRD NFR-Cost), which makes rate limiting the
normal case rather than an edge case. Two independent recovery axes:

- **Key rotation.** Several API keys are configured; a 429/401 on one key retries
  the same model on the next key before giving up on the model.
- **Model fallback.** When every key is rate-limited for a model, the next model in
  the fallback chain is tried. Free capacity moves around during the day; a single
  hardcoded model would make the system look broken when it is merely throttled.

When both axes are exhausted the call raises. There is deliberately no offline
fallback: a fabricated answer that looks exactly like a real analysis is more
dangerous than a visible failure, because a human trusts the output at exactly the
moment it is least trustworthy. The worker catches the error, leaves the incident
in `investigating`, and the reconciliation sweep retries it when capacity returns
(ESD §12 — degrade the *throughput*, never the *truthfulness*).

Keys are read from settings (env) and never logged: log lines carry a key *index*,
never the value (CLAUDE.md §12).
"""

from __future__ import annotations

from typing import Any

import httpx

from core.config import get_settings
from core.logging import get_logger
from providers.base import LLMProvider, LLMResult
from providers.parsing import strip_code_fences


class RateLimited(Exception):
    """Upstream said 429 (or the key was rejected) — try another key/model."""


class ProviderExhausted(RuntimeError):
    """Every configured model and key was unavailable. No answer is better than a fake one."""


class DailyQuotaExhausted(ProviderExhausted):
    """The account's free-model daily allowance is spent — distinct from momentary throttling.

    Worth its own type because the operator response is completely different: a
    per-minute throttle clears itself in seconds and the retry sweep handles it,
    whereas a daily cap needs either a wait until the UTC reset or credits on the
    account. Collapsing both into "rate limited" sends people to re-run a command
    that cannot possibly succeed for hours.
    """


class OpenRouterProvider(LLMProvider):
    """OpenAI-compatible chat completions with key rotation and model fallback."""

    name = "openrouter"

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        settings = get_settings()
        self._keys = settings.openrouter_key_list
        if not self._keys:
            raise ValueError(
                "LLM_PROVIDER=openrouter but OPENROUTER_API_KEYS is empty; "
                "set it in .env (never commit it)"
            )
        self._base_url = settings.openrouter_base_url
        self._temperature = settings.llm_temperature
        self._max_tokens = settings.llm_max_tokens
        self._model_for_rca = settings.llm_model_rca
        self._model_default = settings.llm_model_default
        self._fallbacks = settings.llm_fallback_list
        self._max_truncation_budget = settings.llm_max_tokens_on_truncation
        self._http = http or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
        self._key_cursor = 0
        self._log = get_logger(component="llm.openrouter")

    def _models_for(self, agent: str) -> list[str]:
        """Primary model for the agent, then the shared fallback chain."""
        primary = self._model_for_rca if agent == "rca" else self._model_default
        chain = [primary]
        for model in self._fallbacks:
            if model not in chain:
                chain.append(model)
        return chain

    async def _post(
        self, model: str, key_index: int, prompt: str, agent: str, max_tokens: int
    ) -> dict[str, Any]:
        key = self._keys[key_index % len(self._keys)]
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self._temperature,
            "max_tokens": max_tokens,
        }
        if agent == "rca":
            # RCA output is parsed as strict JSON; ask the API to enforce it.
            payload["response_format"] = {"type": "json_object"}
        response = await self._http.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # OpenRouter attribution headers; harmless and improve rate limits.
                "HTTP-Referer": "https://github.com/Saravankumar25/aegis",
                "X-Title": "Aegis Incident Response",
            },
        )
        if response.status_code in (401, 402, 429):
            body_text = response.text[:400]
            if "free-models-per-day" in body_text or "per-day" in body_text:
                # A daily cap will not clear on retry; say so precisely.
                raise DailyQuotaExhausted(
                    "OpenRouter free-model DAILY quota is exhausted for every configured "
                    "key. This does not clear on retry: wait for the UTC-midnight reset, "
                    "add credits to the account to raise the cap, or point "
                    "LLM_MODEL_RCA/LLM_MODEL_FALLBACKS at a paid model."
                )
            raise RateLimited(
                f"{model} key#{key_index % len(self._keys)} -> {response.status_code}"
            )
        if response.status_code >= 500:
            raise RateLimited(f"{model} upstream {response.status_code}")
        response.raise_for_status()
        return response.json()

    async def complete(
        self, prompt: str, *, agent: str, ensemble_pass: int = 0, max_tokens: int = 1024
    ) -> LLMResult:
        """Call the model, walking the model × key matrix until one answers."""
        attempts = 0
        # Reasoning models spend a large, unpredictable share of the completion
        # budget on hidden thinking tokens; too small a budget truncates the JSON
        # mid-object and silently costs an entire ensemble pass.
        budget = max(max_tokens, self._max_tokens)
        for model in self._models_for(agent):
            for offset in range(len(self._keys)):
                key_index = self._key_cursor + offset
                attempts += 1
                try:
                    body = await self._post(model, key_index, prompt, agent, budget)
                except DailyQuotaExhausted:
                    # Rotating keys/models cannot help: the cap is account-wide and daily.
                    self._log.error("llm_daily_quota_exhausted", agent=agent)
                    raise
                except RateLimited as exc:
                    self._log.warning("llm_throttled", detail=str(exc), agent=agent)
                    continue
                except (httpx.HTTPError, OSError) as exc:
                    self._log.warning(
                        "llm_transport_error", model=model, agent=agent, error=str(exc)
                    )
                    continue

                choices = body.get("choices") or []
                if not choices:
                    self._log.warning("llm_empty_choices", model=model, agent=agent)
                    continue
                finish = choices[0].get("finish_reason")
                text = choices[0].get("message", {}).get("content") or ""
                if finish == "length" and budget < self._max_truncation_budget:
                    # Output was cut off: retry the same model once with a bigger
                    # budget rather than losing the pass to a formatting artefact.
                    self._log.warning("llm_truncated", model=model, agent=agent, budget=budget)
                    budget = self._max_truncation_budget
                    body = await self._post(model, key_index, prompt, agent, budget)
                    choices = body.get("choices") or []
                    if not choices:
                        continue
                    text = choices[0].get("message", {}).get("content") or ""
                if not text.strip():
                    # Some reasoning models burn the whole budget on hidden thinking
                    # and return an empty message; treat as a miss, try the next.
                    self._log.warning("llm_empty_content", model=model, agent=agent)
                    continue

                usage = body.get("usage") or {}
                # Successful key becomes the starting point for the next call, so a
                # throttled key isn't retried first every single time.
                self._key_cursor = key_index
                self._log.info(
                    "llm_call",
                    model=model,
                    agent=agent,
                    ensemble_pass=ensemble_pass,
                    tokens=usage.get("total_tokens"),
                    attempts=attempts,
                )
                return LLMResult(
                    text=strip_code_fences(text),
                    model=model,
                    tokens_used=int(usage.get("total_tokens") or 0),
                    cost_usd=float(usage.get("cost") or 0.0),
                    latency_ms=0,
                )

        self._log.error("llm_all_models_exhausted", agent=agent, attempts=attempts)
        raise ProviderExhausted(
            f"every configured model and key was unavailable for agent '{agent}' "
            f"after {attempts} attempts; the incident is left for the retry sweep "
            f"rather than answered with fabricated reasoning"
        )

    async def aclose(self) -> None:
        await self._http.aclose()
