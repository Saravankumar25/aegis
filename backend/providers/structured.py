"""Provider-independent structured-output enforcement (ESD §20).

Every provider needs the same three layers, because no model honours any single one
of them reliably on its own:

1. the API is *asked* to constrain generation to the schema (provider-specific, and
   only ever an optimisation),
2. the response is *parsed* tolerantly — fenced blocks and prose-wrapped objects are
   routine even when a schema was requested,
3. the result is *validated*, and only a validated object is ever returned.

Repair feeds the validation error back to the model rather than simply re-rolling.
A blind retry re-samples the same misunderstanding; naming the offending field is
what actually changes the outcome.

This lives outside the provider modules so that adding a provider cannot quietly ship
a weaker version of the guarantee. The schema contract is a safety property — callers
branch on these fields — so it must not vary by which vendor answered.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from providers.base import LLMResult, StructuredResult
from providers.errors import StructuredOutputError
from providers.parsing import parse_json_object


class _Logger(Protocol):
    def info(self, event: str, **kw: Any) -> None: ...
    def warning(self, event: str, **kw: Any) -> None: ...


CompleteFn = Callable[..., Awaitable[LLMResult]]


def summarize_validation_error(exc: ValidationError) -> str:
    """Render a Pydantic error compactly enough to hand back to a model.

    The full repr runs to hundreds of characters of URLs and input echoes; a model
    repairs better from `field: message` than from a wall of diagnostics, and the
    echoed input can reintroduce the very text that failed.
    """
    parts = []
    for err in exc.errors()[:6]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {err.get('msg', 'invalid')}")
    return "; ".join(parts)


async def complete_structured_with_repair[StructuredT: BaseModel](
    complete: CompleteFn,
    prompt: str,
    *,
    schema: type[StructuredT],
    agent: str,
    log: _Logger,
    repair_attempts: int,
    system: str | None = None,
    ensemble_pass: int = 0,
    max_tokens: int = 1024,
    prompt_ref: str | None = None,
) -> StructuredResult[StructuredT]:
    """Drive ``complete`` until it yields a valid ``schema`` instance, or raise.

    ``complete`` must accept a ``json_schema`` keyword so a provider that supports
    native constrained decoding can use it; providers that do not may ignore it.
    Token and cost totals accumulate across repairs, so an expensive repair loop is
    visible in the incident's budget rather than being billed invisibly.
    """
    json_schema = schema.model_json_schema()
    attempt_prompt = prompt
    last_error = ""
    total_tokens = 0
    total_cost = 0.0
    started = time.perf_counter()

    for repair in range(repair_attempts + 1):
        result = await complete(
            attempt_prompt,
            agent=agent,
            system=system,
            ensemble_pass=ensemble_pass,
            max_tokens=max_tokens,
            prompt_ref=prompt_ref,
            json_schema=json_schema,
        )
        total_tokens += result.tokens_used
        total_cost += result.cost_usd

        payload = parse_json_object(result.text)
        if payload is not None:
            try:
                value = schema.model_validate(payload)
            except ValidationError as exc:
                last_error = summarize_validation_error(exc)
            else:
                if repair:
                    log.info(
                        "llm_structured_repaired",
                        agent=agent,
                        schema=schema.__name__,
                        attempts=repair,
                    )
                return StructuredResult(
                    value=value,
                    result=LLMResult(
                        text=result.text,
                        model=result.model,
                        tokens_used=total_tokens,
                        cost_usd=total_cost,
                        latency_ms=int((time.perf_counter() - started) * 1000),
                        prompt_ref=prompt_ref,
                        repair_attempts=repair,
                    ),
                )
        else:
            last_error = "response was not a JSON object"

        log.warning(
            "llm_structured_invalid",
            agent=agent,
            schema=schema.__name__,
            attempt=repair + 1,
            error=last_error,
        )
        attempt_prompt = (
            f"{prompt}\n\n"
            f"Your previous response was rejected: {last_error}\n"
            f"Respond with JSON only — no prose, no markdown fences — matching exactly "
            f"this JSON Schema:\n{json.dumps(json_schema)}"
        )

    raise StructuredOutputError(
        f"agent '{agent}' could not produce valid {schema.__name__} after "
        f"{repair_attempts + 1} attempts; last error: {last_error}"
    )
