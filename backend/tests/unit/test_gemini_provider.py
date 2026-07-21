"""Contract tests: the Gemini provider's resilience and refusal behaviour (ESD §20, §22).

These run against `httpx.MockTransport`, never a live model, so they can assert the
exact recovery sequence — which is the part that matters. Free-tier capacity is
unreliable by default, so "what happens when upstream says no" is the provider's main
job, not an edge case.

The load-bearing assertion throughout: **the provider never returns text it did not
receive from a model.** A blocked, empty, or throttled response must become a retry or
an exception, never an answer, because fabricated reasoning is indistinguishable from
real reasoning at exactly the point a human trusts it (CLAUDE.md §18).
"""

from __future__ import annotations

import json

import httpx
import pytest

from core.config import get_settings
from providers.errors import DailyQuotaExhausted, ProviderExhausted
from providers.gemini import GeminiProvider

pytestmark = pytest.mark.anyio

KEYS = ["key-alpha", "key-bravo", "key-charlie"]
PRIMARY = "model-primary"
SECOND = "model-second"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def gemini_settings(monkeypatch):
    """Point settings at a deterministic key set and model chain."""
    monkeypatch.setenv("GEMINI_API_KEYS", ",".join(KEYS))
    monkeypatch.setenv("GEMINI_MODEL_DEFAULT", PRIMARY)
    monkeypatch.setenv("GEMINI_MODEL_RCA", PRIMARY)
    monkeypatch.setenv("GEMINI_MODEL_FALLBACKS", f"{PRIMARY},{SECOND}")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ok(text: str, *, finish: str = "STOP", tokens: int = 42) -> dict:
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": finish},
        ],
        "usageMetadata": {"totalTokenCount": tokens},
    }


def _provider(handler) -> GeminiProvider:
    return GeminiProvider(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _model_of(request: httpx.Request) -> str:
    # .../models/{model}:generateContent
    return request.url.path.rsplit("/", 1)[-1].split(":")[0]


# --- recovery axes ------------------------------------------------------------------------


async def test_throttled_key_rotates_to_the_next_key():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-goog-api-key"])
        if len(seen) < 3:
            return httpx.Response(429, text="RESOURCE_EXHAUSTED")
        return httpx.Response(200, json=_ok("recovered"))

    result = await _provider(handler).complete("p", agent="triage")
    assert result.text == "recovered"
    assert seen == KEYS, "each 429 must move to a different key, not retry the same one"


async def test_model_falls_back_when_every_key_is_throttled():
    attempted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = _model_of(request)
        attempted.append(model)
        if model == PRIMARY:
            return httpx.Response(503, text="high demand")
        return httpx.Response(200, json=_ok("from fallback"))

    result = await _provider(handler).complete("p", agent="triage")
    assert result.text == "from fallback"
    assert result.model == SECOND
    # Every key tried on the primary before giving up on that model.
    assert attempted[: len(KEYS)] == [PRIMARY] * len(KEYS)


async def test_exhaustion_raises_rather_than_fabricating():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="RESOURCE_EXHAUSTED")

    with pytest.raises(ProviderExhausted):
        await _provider(handler).complete("p", agent="triage")


_DAILY_CAP = '{"error":{"message":"Quota exceeded: requests per day"}}'


async def test_one_key_over_daily_cap_rotates_to_the_others():
    """Gemini keys carry independent per-project quotas.

    Treating the first capped key as terminal — which is correct for OpenRouter, whose
    keys bill against one account — stranded every remaining key and reported the system
    as out of capacity while most of it was idle.
    """
    used: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-goog-api-key"]
        used.append(key)
        if key == KEYS[0]:
            return httpx.Response(429, text=_DAILY_CAP)
        return httpx.Response(200, json=_ok("answered by a key with quota left"))

    result = await _provider(handler).complete("p", agent="triage")
    assert result.text == "answered by a key with quota left"
    assert used[0] == KEYS[0] and used[1] == KEYS[1]


async def test_daily_quota_raised_only_when_every_key_is_capped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text=_DAILY_CAP)

    with pytest.raises(DailyQuotaExhausted, match="all 3 Gemini keys"):
        await _provider(handler).complete("p", agent="triage")


async def test_capped_key_is_not_retried_on_the_next_model():
    """Once a key is known capped, spending an attempt on it per model is pure latency."""
    attempts: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["x-goog-api-key"]
        attempts.append((_model_of(request), key))
        if key == KEYS[0]:
            return httpx.Response(429, text=_DAILY_CAP)
        return httpx.Response(503, text="high demand")

    provider = _provider(handler)
    with pytest.raises(ProviderExhausted):
        await provider.complete("p", agent="triage")

    capped_attempts = [a for a in attempts if a[1] == KEYS[0]]
    assert len(capped_attempts) == 1, (
        f"the capped key was retried {len(capped_attempts)} times; it should be skipped "
        "after the first daily-cap response"
    )


async def test_capped_key_is_reused_after_a_success_proves_it_wrong():
    """State is a cache, not a verdict: a key that answers is healthy by definition."""
    provider = _provider(lambda r: httpx.Response(200, json=_ok("fine")))
    provider._pool.mark_quota_exhausted(0)
    assert 0 not in provider._pool.order()
    provider._pool.mark_success(0)
    assert provider._pool.order()[0] == 0


# --- refusals must never become answers ---------------------------------------------------


async def test_safety_blocked_response_is_not_returned_as_an_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        if _model_of(request) == PRIMARY:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {"content": {"parts": [{"text": "partial"}]}, "finishReason": "SAFETY"}
                    ]
                },
            )
        return httpx.Response(200, json=_ok("clean answer"))

    result = await _provider(handler).complete("p", agent="rca")
    assert result.text == "clean answer"
    assert result.model == SECOND


async def test_blocked_prompt_moves_on_rather_than_returning_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        if _model_of(request) == PRIMARY:
            return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
        return httpx.Response(200, json=_ok("second model was fine"))

    result = await _provider(handler).complete("p", agent="rca")
    assert result.text == "second model was fine"


async def test_empty_content_is_treated_as_a_miss():
    """Thinking-heavy models can spend the budget and return no parts."""

    def handler(request: httpx.Request) -> httpx.Response:
        if _model_of(request) == PRIMARY:
            return httpx.Response(200, json={"candidates": [{"content": {"parts": []}}]})
        return httpx.Response(200, json=_ok("real text"))

    assert (await _provider(handler).complete("p", agent="rca")).text == "real text"


# --- response shape -----------------------------------------------------------------------


async def test_multi_part_text_is_concatenated_not_truncated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "alpha "}, {"text": "bravo "}, {"text": "charlie"}]
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {"totalTokenCount": 9},
            },
        )

    result = await _provider(handler).complete("p", agent="rca")
    assert result.text == "alpha bravo charlie", "taking parts[0] would silently truncate"


async def test_truncated_output_is_retried_with_a_larger_budget():
    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        budgets.append(body["generationConfig"]["maxOutputTokens"])
        if len(budgets) == 1:
            return httpx.Response(200, json=_ok("cut off", finish="MAX_TOKENS"))
        return httpx.Response(200, json=_ok("complete answer"))

    result = await _provider(handler).complete("p", agent="rca", max_tokens=100)
    assert result.text == "complete answer"
    assert budgets[1] > budgets[0], "truncation must raise the budget, not re-ask identically"


async def test_system_prompt_goes_to_system_instruction_field():
    """Aegis's injection resistance depends on system weighting, which Gemini only
    applies to `systemInstruction` — not to a system-ish string inside `contents`."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_ok("ok"))

    await _provider(handler).complete("user text", agent="rca", system="STANDING CONTRACT")
    assert captured["systemInstruction"]["parts"][0]["text"] == "STANDING CONTRACT"
    assert "STANDING CONTRACT" not in json.dumps(captured["contents"])


async def test_structured_request_sends_translated_schema():
    from pydantic import BaseModel

    class Out(BaseModel):
        verdict: str

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_ok('{"verdict":"approve"}'))

    result = await _provider(handler).complete_structured("p", schema=Out, agent="observer")
    assert result.value.verdict == "approve"
    schema = captured["generationConfig"]["responseSchema"]
    assert schema["type"] == "OBJECT"
    assert schema["properties"]["verdict"]["type"] == "STRING"
    assert captured["generationConfig"]["responseMimeType"] == "application/json"


# --- credential hygiene -------------------------------------------------------------------


async def test_api_key_is_never_logged(capsys):
    """CLAUDE.md §12: log lines carry a key index, never the value.

    Uses `capsys`, not `caplog`: the project configures structlog with a
    `PrintLoggerFactory`, which writes to stdout and never reaches stdlib logging, so a
    `caplog`-based version of this test captures nothing and can never fail. The
    non-empty assertion below exists to keep it that way — if logging is rewired again,
    this fails loudly rather than silently going vacuous.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["x-goog-api-key"] == KEYS[0]:
            return httpx.Response(429, text="throttled")
        return httpx.Response(200, json=_ok("fine"))

    await _provider(handler).complete("p", agent="rca")
    logged = capsys.readouterr().out

    assert "llm_throttled" in logged, (
        "expected the throttle to be logged; if nothing was captured this test is "
        "vacuous and proves nothing about credential hygiene"
    )
    for key in KEYS:
        assert key not in logged, "an API key value reached the logs"
    # The index is what makes a throttled key diagnosable without exposing it.
    assert "key#" in logged
