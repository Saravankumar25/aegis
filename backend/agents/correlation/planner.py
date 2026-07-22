"""Correlation Agent: LLM-driven, iterative evidence gathering (FR-2.1..FR-2.3).

Replaces a fixed five-call sequence that ran identically for every incident. That sequence
could not follow a lead: it never fetched a *specific* pod's detail because it never knew
which pod was unhealthy until after it had finished, and it asked the same PromQL question
whether the symptom was latency or crashes.

The loop is: **plan → dispatch → observe → re-plan**, until the model says it has enough or a
bound is hit. Each round the model sees what it has already gathered, so round two can act on
what round one found — which is the entire difference between gathering evidence and
collecting it.

Three bounds keep an outage from becoming an unbounded agent loop. None of them are advisory:

* ``max_rounds`` — planning iterations.
* ``max_calls_per_round`` — breadth per iteration.
* a **duplicate-call guard** — the same tool with the same arguments is never dispatched
  twice. Without it, a model that likes an informative tool re-requests it every round and
  the loop spends its budget re-reading one thing.

Tool *dispatch* stays deterministic Python: the model chooses names and arguments, and this
module validates them against the read-only allowlist before anything executes. A model can
therefore never reach a write tool, whatever a prompt-injected log line asks it to do.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from agents.correlation.tools import TOOLS_BY_NAME, render_catalog, validate_call
from agents.evidence import EvidenceStore
from agents.prompts.library import CORRELATION_PLAN, CORRELATION_SYNTHESIS
from agents.topology import dependencies_of, dependents_of
from core.config import get_settings
from core.logging import get_logger
from guardrails import guard_input, guard_output
from providers.base import LLMResult
from redaction.pipeline import EVIDENCE_RULES

_log = get_logger(component="agent.correlation")


class ToolCall(BaseModel):
    """One tool the model wants invoked."""

    tool: str = Field(description="Exact tool name from the catalog, e.g. 'k8s.get_pod'.")
    arguments: dict[str, Any] = Field(default_factory=dict)
    why: str = Field(
        default="", description="What this call would establish that is not yet known."
    )


class GatheringPlan(BaseModel):
    """The model's decision for one round."""

    reasoning: str = Field(
        default="", description="Brief statement of what is still unknown.", max_length=1000
    )
    calls: list[ToolCall] = Field(default_factory=list)
    done: bool = Field(
        default=False, description="True when the evidence gathered is sufficient for RCA."
    )


class CorrelationSynthesis(BaseModel):
    """The correlated picture handed to RCA."""

    summary: str = Field(description="What the evidence shows, with evidence ids.", max_length=2500)
    signals: list[str] = Field(
        default_factory=list, description="Distinct observations, each citing an evidence id."
    )
    contradictions: list[str] = Field(
        default_factory=list,
        description="Observations pointing in different directions. Empty if none.",
    )
    change_detected: bool = Field(
        default=False, description="Whether a deploy/change inside the window is evidenced."
    )


class CorrelationOutcome(BaseModel):
    """Everything the correlation node produces."""

    model_config = {"arbitrary_types_allowed": True}

    store: EvidenceStore
    summary_json: str
    synthesis: CorrelationSynthesis | None = None
    rounds_used: int = 0
    calls_dispatched: int = 0
    calls_rejected: list[str] = Field(default_factory=list)
    tokens_used: int = 0
    cost_usd: float = 0.0
    # Models that actually answered across planning rounds, not the provider name.
    model_used: str | None = None
    latency_ms: int | None = None
    prompt_refs: list[str] = Field(default_factory=list)
    llm_planned: bool = False


def _gathered_digest(store: EvidenceStore) -> str:
    """What the planner sees of what it already has.

    Deliberately a *digest*, not the full evidence: the planner is choosing what to look at
    next, and feeding it every log line it has already gathered wastes budget on content that
    does not change the decision. The synthesis step gets the full text.
    """
    if not store.items:
        return "(nothing gathered yet)"
    return "\n".join(
        f"- {item.id} from {item.source}: {item.summary[:300]}" for item in store.items
    )


async def _dispatch(gateway: Any, store: EvidenceStore, call: ToolCall) -> tuple[bool, str | None]:
    """Execute one validated call, folding the result into the store."""
    spec = TOOLS_BY_NAME[call.tool]
    result = await gateway.call(spec.server, spec.tool, call.arguments)
    if not result.get("ok"):
        # The error string comes from an MCP tool and can carry an upstream response body,
        # so it is untrusted text and is sanitised before it becomes a documented gap.
        # `note_gap` sanitises and hands back the safe text, so the value returned to the
        # caller carries no unredacted upstream response body.
        return False, store.note_gap(call.tool, result.get("error") or "unavailable")

    data = result.get("data")
    if data is None or (isinstance(data, list | dict) and not data):
        # An empty result is a real finding ("nothing shipped in the window"), but it must be
        # phrased so downstream pattern-matching cannot read absence as a change signal.
        text = f"{call.tool} returned no results for the requested scope"
    else:
        text = json.dumps(data, indent=2, default=str) if not isinstance(data, str) else data

    store.add(
        type_=spec.evidence_type,
        source=call.tool,
        ref=f"{spec.server}/{spec.tool}/{json.dumps(call.arguments, sort_keys=True, default=str)}",
        text=text,
    )
    return True, None


async def gather(
    provider: Any,
    gateway: Any,
    *,
    service: str,
    title: str,
    severity: str,
) -> CorrelationOutcome:
    """Run the plan→dispatch→observe loop, then synthesise the correlated picture."""
    settings = get_settings()
    store = EvidenceStore()
    seen: set[str] = set()
    rejected: list[str] = []
    tokens = 0
    cost = 0.0
    # Models that actually answered across planning rounds. A set, because fallback can
    # move rounds onto different models and reporting only the first would misattribute.
    models: set[str] = set()
    latency_total = 0
    refs: list[str] = []
    rounds = 0
    dispatched = 0

    for round_index in range(settings.correlation_max_rounds):
        rounds = round_index + 1
        prompt = CORRELATION_PLAN.render(
            title=title,
            service=service,
            severity=severity,
            depends_on=", ".join(dependencies_of(service)) or "nothing",
            dependents=", ".join(dependents_of(service)) or "nothing",
            tool_catalog=render_catalog(),
            gathered=_gathered_digest(store),
            gaps="\n".join(f"- {g}" for g in store.gaps) or "- none",
            max_calls=settings.correlation_max_calls_per_round,
        )
        guarded = guard_input(prompt, agent="correlation")

        try:
            structured = await provider.complete_structured(
                guarded.prompt,
                schema=GatheringPlan,
                agent="correlation",
                system=CORRELATION_PLAN.system,
                prompt_ref=CORRELATION_PLAN.ref,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to the baseline sweep
            _log.warning("correlation_planning_unavailable", round=rounds, error=str(exc))
            if not store.items:
                # Nothing gathered at all: fall back so RCA still receives evidence rather
                # than an empty store, which it would correctly refuse to reason from.
                await _baseline_sweep(gateway, store, service)
            break

        plan = structured.value
        tokens += structured.result.tokens_used
        cost += structured.result.cost_usd
        models.add(structured.result.model)
        latency_total += structured.result.latency_ms
        if structured.result.prompt_ref:
            refs.append(structured.result.prompt_ref)

        _log.info(
            "correlation_plan",
            round=rounds,
            calls=len(plan.calls),
            done=plan.done,
            reasoning=plan.reasoning[:200],
        )

        if plan.done and store.items:
            break
        if not plan.calls:
            # Not done but nothing proposed — the model has stalled. Continuing would spend
            # another round producing the same empty plan.
            if not store.items:
                await _baseline_sweep(gateway, store, service)
            break

        for call in plan.calls[: settings.correlation_max_calls_per_round]:
            error = validate_call(call.tool, call.arguments)
            if error:
                rejected.append(error)
                _log.warning("correlation_call_rejected", tool=call.tool, reason=error)
                continue
            fingerprint = f"{call.tool}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
            if fingerprint in seen:
                rejected.append(f"'{call.tool}' already called with these arguments")
                continue
            seen.add(fingerprint)
            await _dispatch(gateway, store, call)
            dispatched += 1

    if not store.items and not store.gaps:
        await _baseline_sweep(gateway, store, service)

    synthesis, synth_result = await _synthesise(provider, store, service=service, title=title)
    synth_ref = synth_result.prompt_ref if synth_result else None
    if synth_result is not None:
        tokens += synth_result.tokens_used
        cost += synth_result.cost_usd
        models.add(synth_result.model)
        latency_total += synth_result.latency_ms
    if synth_ref:
        refs.append(synth_ref)

    correlation = {
        "service": service,
        "topology": {
            "depends_on": dependencies_of(service),
            "depended_on_by": dependents_of(service),
        },
        "temporal": {
            "deploy_lookback_hours": settings.deploy_lookback_hours,
            "change_detected": bool(synthesis and synthesis.change_detected),
        },
        "evidence_ids": [i.id for i in store.items],
        "gaps": store.gaps,
        "signals": synthesis.signals if synthesis else [],
        "contradictions": synthesis.contradictions if synthesis else [],
        "summary": synthesis.summary if synthesis else "",
    }

    return CorrelationOutcome(
        store=store,
        summary_json=json.dumps(correlation),
        synthesis=synthesis,
        rounds_used=rounds,
        calls_dispatched=dispatched,
        calls_rejected=rejected,
        tokens_used=tokens,
        cost_usd=cost,
        # Summed, not maxed: correlation rounds are sequential, so the total is the time
        # a human actually waited.
        latency_ms=latency_total or None,
        model_used=",".join(sorted(models)) or None,
        prompt_refs=refs,
        llm_planned=bool(refs),
    )


async def _synthesise(
    provider: Any, store: EvidenceStore, *, service: str, title: str
) -> tuple[CorrelationSynthesis | None, LLMResult | None]:
    """Turn gathered evidence into a correlated picture.

    Returns the raw `LLMResult` rather than a widening tuple of extracted fields: the
    caller needs model, latency, tokens, cost and prompt_ref, and threading five
    positional values through made it easy to add an accounting field here and forget to
    record it there — which is how `model_used` came to hold the provider name.
    """
    if not store.items:
        return None, None

    settings = get_settings()
    prompt = CORRELATION_SYNTHESIS.render(
        evidence_rules=EVIDENCE_RULES,
        title=title,
        service=service,
        depends_on=", ".join(dependencies_of(service)) or "nothing",
        dependents=", ".join(dependents_of(service)) or "nothing",
        lookback_hours=settings.deploy_lookback_hours,
        evidence_block=store.prompt_block(),
        gaps="\n".join(f"- {g}" for g in store.gaps) or "- none",
    )
    guarded = guard_input(prompt, agent="correlation")

    try:
        structured = await provider.complete_structured(
            guarded.prompt,
            schema=CorrelationSynthesis,
            agent="correlation",
            system=CORRELATION_SYNTHESIS.system,
            prompt_ref=CORRELATION_SYNTHESIS.ref,
        )
    except Exception as exc:  # noqa: BLE001 — RCA can still reason from raw evidence
        _log.warning("correlation_synthesis_unavailable", error=str(exc))
        return None, None

    guard_output(structured.value.summary, agent="correlation", require_grounding=True)
    return structured.value, structured.result


async def _baseline_sweep(gateway: Any, store: EvidenceStore, service: str) -> None:
    """Deterministic minimum sweep, used only when planning is unavailable.

    Not a second implementation of correlation — it exists so an LLM outage degrades to the
    old behaviour instead of handing RCA an empty store, which RCA would (correctly) refuse
    to reason from. Kept deliberately small.
    """
    from agents.correlation.collector import collect_evidence

    _log.info("correlation_baseline_sweep", service=service)
    fallback_store, _ = await collect_evidence(gateway, service)
    store.items.extend(fallback_store.items)
    store.gaps.extend(fallback_store.gaps)
