"""LangSmith tracing for the agent pipeline (ESD §13).

Structured logs answer "what happened". Traces answer "why did this incident produce *that*
hypothesis" — which prompt version ran, what evidence the model actually saw, which tools it
chose and what they returned, how many repair attempts the schema needed, where the latency
went. Reconstructing that from log lines across seven agents and three ensemble passes is the
debugging task this exists to remove.

Two design rules, both load-bearing:

**Tracing never breaks incident response.** Every entry point degrades to a no-op if LangSmith
is unconfigured or unreachable. An observability backend outage must not stall an
investigation — that would make the tool that explains failures a cause of them.

**Traces inherit the redaction pipeline.** Evidence reaching a trace is already redacted and
delimited, because it is the same text that reached the prompt. But trace payloads leave the
process and land in a third-party service, so anything added here that did *not* come through
`EvidenceStore` must be screened first (CLAUDE.md §12, §17). The helpers below take already-
redacted text by construction rather than raw tool output.
"""

from __future__ import annotations

import contextlib
import functools
import os
import sys
from collections.abc import Callable, Iterator
from typing import Any

from core.config import get_settings
from core.logging import get_logger

_log = get_logger(component="tracing")

_client: Any | None = None
_enabled: bool | None = None


def tracing_enabled() -> bool:
    """True when LangSmith is configured. Resolved once, then cached."""
    global _enabled, _client
    if _enabled is not None:
        return _enabled

    settings = get_settings()
    if not settings.langsmith_api_key:
        _enabled = False
        return False
    try:
        # The SDK's `traceable`/`trace` helpers read the *process environment*, not any
        # Client instance handed to them, and they no-op unless LANGSMITH_TRACING is
        # truthy. pydantic-settings loads `.env` into a settings object without exporting
        # it, so without this bridge every span silently did nothing — the code looked
        # correctly wired, `tracing_enabled()` returned True, and no run was ever
        # submitted. Set before importing langsmith so the module reads final values.
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        os.environ.setdefault("LANGSMITH_API_KEY", settings.langsmith_api_key)
        os.environ.setdefault("LANGSMITH_ENDPOINT", settings.langsmith_endpoint)
        os.environ.setdefault("LANGSMITH_PROJECT", settings.langsmith_project)

        from langsmith import Client

        _client = Client(
            api_key=settings.langsmith_api_key,
            api_url=settings.langsmith_endpoint,
        )
        _enabled = True
        _log.info("langsmith_enabled", project=settings.langsmith_project)
    except Exception as exc:  # noqa: BLE001 — tracing is never worth failing a run over
        _log.warning("langsmith_unavailable", error=str(exc))
        _enabled = False
    return _enabled


def flush(timeout_seconds: float = 10.0) -> None:
    """Block until queued runs are submitted.

    The SDK batches submissions on a background thread, so a short-lived process — the
    worker finishing an incident, a CLI verification run — can exit with traces still
    queued and drop them. Long-running servers do not need this; scripts do.
    """
    if not tracing_enabled() or _client is None:
        return
    try:
        _client.flush()
    except Exception as exc:  # noqa: BLE001 — a failed flush must not fail the caller
        _log.warning("langsmith_flush_failed", error=str(exc))


def traced[F: Callable[..., Any]](name: str, run_type: str = "chain") -> Callable[[F], F]:
    """Decorate an agent step so it appears as a span.

    When tracing is off this returns the function unchanged — no wrapper, no overhead, and
    no behavioural difference between a traced and untraced deployment.
    """

    def decorator(func: F) -> F:
        if not tracing_enabled():
            return func
        try:
            from langsmith import traceable

            return traceable(run_type=run_type, name=name)(func)  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001
            _log.warning("langsmith_decorator_failed", name=name, error=str(exc))
            return func

    return decorator


@contextlib.contextmanager
def trace_span(name: str, *, run_type: str = "chain", **metadata: Any) -> Iterator[None]:
    """Trace a block that is not a whole function (a tool call, one ensemble pass).

    Enter and exit are guarded individually, and `yield` happens exactly once on every
    path. The obvious shape — `try: with trace(...): yield / except Exception: yield` —
    is a real bug rather than a style preference: an exception raised by the *body* is
    thrown into the generator at the yield, caught by that `except`, and answered with a
    second yield, which contextlib rejects with `RuntimeError: generator didn't stop
    after throw()`. The caller's real exception is destroyed and replaced by that
    RuntimeError, so `ProviderExhausted` stops matching `except ProviderExhausted` and
    the degradation path silently stops working — but only once tracing is switched on,
    which is why it survived until LangSmith was first genuinely enabled.

    `sys.exc_info()` is forwarded to `__exit__` so a failing block is recorded as a
    failed span rather than a successful one.
    """
    if not tracing_enabled():
        yield
        return

    span: Any | None = None
    try:
        from langsmith.run_helpers import trace

        span = trace(name=name, run_type=run_type, metadata=metadata)
        span.__enter__()
    except Exception as exc:  # noqa: BLE001 — tracing must not break the traced code
        _log.warning("langsmith_span_failed", name=name, error=str(exc))
        span = None

    try:
        yield
    finally:
        if span is not None:
            try:
                span.__exit__(*sys.exc_info())
            except Exception as exc:  # noqa: BLE001
                _log.warning("langsmith_span_exit_failed", name=name, error=str(exc))


def annotate(**fields: Any) -> None:
    """Attach metadata to the current span (token counts, prompt refs, verdicts).

    Silently does nothing outside a span, which is the desired behaviour: call sites should
    not have to know whether they were invoked inside a traced pipeline or a unit test.
    """
    if not tracing_enabled():
        return
    try:
        from langsmith.run_helpers import get_current_run_tree

        run = get_current_run_tree()
        if run is not None:
            run.extra.setdefault("metadata", {}).update(fields)
    except Exception as exc:  # noqa: BLE001
        _log.debug("langsmith_annotate_failed", error=str(exc))


def trace_incident(incident_id: str, service: str, title: str) -> dict[str, Any]:
    """Config passed to `graph.ainvoke` so a whole investigation is one trace.

    `incident_id` becomes both a tag and metadata so a production incident can be found in
    LangSmith by the same id used everywhere else in the system (CLAUDE.md §17).
    """
    if not tracing_enabled():
        return {}
    settings = get_settings()
    return {
        "run_name": f"incident:{service}",
        "project_name": settings.langsmith_project,
        "tags": [f"incident:{incident_id}", f"service:{service}"],
        "metadata": {
            "incident_id": incident_id,
            "service_name": service,
            "title": title[:200],
        },
    }


def reset_for_tests() -> None:
    """Clear cached state so a test can toggle configuration."""
    global _enabled, _client
    _enabled = None
    _client = None


def wrap_llm_call[F: Callable[..., Any]](func: F) -> F:
    """Mark a provider method as an `llm` run so token/latency panels populate.

    Applied at the provider rather than at each agent: it is the only place that knows the
    model actually used after fallback, which is precisely the field that matters when
    diagnosing why one incident behaved differently from another.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not tracing_enabled():
            return await func(*args, **kwargs)
        with trace_span(
            f"llm:{kwargs.get('agent', 'unknown')}",
            run_type="llm",
            agent=kwargs.get("agent"),
            prompt_ref=kwargs.get("prompt_ref"),
            ensemble_pass=kwargs.get("ensemble_pass"),
        ):
            result = await func(*args, **kwargs)
            annotate(
                model=getattr(result, "model", None),
                tokens_used=getattr(result, "tokens_used", None),
                cost_usd=getattr(result, "cost_usd", None),
                latency_ms=getattr(result, "latency_ms", None),
                repair_attempts=getattr(result, "repair_attempts", 0),
            )
            return result

    return wrapper  # type: ignore[return-value]
