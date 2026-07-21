"""Memory Agent: LLM relevance judgement and lesson writing (FR-7, ESD §9 step 7).

Persistence, the approval gate and the retrieval scope stay in `memory/store.py`. This
module supplies the two judgements that were previously not judgements at all:

* **What to remember.** `draft_summary` assembled its fields by assignment —
  `symptom = incident.title` (the alert string, which is what a *monitor* saw, not what a
  *human* would recognise), `root_cause = hypothesis` verbatim, `fix` a join of action rows.
  A future responder searching by symptom would have to guess the original alert's wording.
* **What to recall.** `recall` returned the three most recent approved memories for the
  service. Recency is not relevance: an unrelated memory surfaced as precedent actively
  drags an investigation toward the wrong cause, and "same service" is the weakest possible
  similarity signal.

**The approval gate is not negotiable and is not here.** The SQL in `store.recall` filters
to `approved_by IS NOT NULL`, and this module only ever *narrows* that candidate set. A
model can decline to surface an approved memory; it can never surface an unapproved one,
because it never sees one. Selection is by index into the candidate list rather than by
free text, so a hallucinated id resolves to nothing instead of to the wrong memory.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agents.accounting import LlmAccounting
from agents.prompts.library import MEMORY_RECALL, MEMORY_SUMMARY
from core.logging import get_logger
from db.models import MemorySummary
from guardrails import guard_input
from providers.base import LLMProvider

_log = get_logger(component="agent.memory")


class MemoryLesson(BaseModel):
    """The reusable lesson distilled from a resolved incident."""

    symptom: str = Field(
        description=(
            "What an engineer would observe, in the words they would search by — not the "
            "alert's wording."
        ),
        max_length=400,
    )
    root_cause: str = Field(description="What actually caused it.", max_length=600)
    fix: str = Field(description="What resolved it, or what was attempted.", max_length=600)
    outcome: str = Field(description="How it ended.", max_length=200)
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence that the recorded cause is correct. Low when inconclusive.",
    )


class RecallSelection(BaseModel):
    """Which candidate memories genuinely inform the current incident."""

    selected: list[int] = Field(
        default_factory=list,
        description="1-based indices of relevant candidates. Empty is a valid answer.",
    )
    reasoning: str = Field(
        default="", description="Why these apply to the current symptoms.", max_length=800
    )


class RecallOutcome(LlmAccounting):
    """Filtered memories plus the reasoning that filtered them."""

    memories: list[MemorySummary] = Field(default_factory=list)
    considered: int = 0
    reasoning: str = ""
    degraded: bool = False

    model_config = {"arbitrary_types_allowed": True}


def _render_candidates(candidates: list[MemorySummary]) -> str:
    return "\n".join(
        f"{i}. [{m.incident_type}] symptom: {m.symptom}\n"
        f"   root cause: {m.root_cause}\n"
        f"   fix: {m.fix}"
        for i, m in enumerate(candidates, start=1)
    )


async def select_relevant(
    provider: LLMProvider | None,
    candidates: list[MemorySummary],
    *,
    title: str,
    service: str,
    kind: str,
    symptoms: str,
) -> RecallOutcome:
    """Narrow already-approved candidates to those that inform this incident.

    Degrades to returning the candidates unfiltered. That is the safe direction: these are
    all human-approved memories for this service, so the worst case is the pre-existing
    recency behaviour, whereas returning nothing would silently drop institutional knowledge
    during exactly the outage it was written for.
    """
    if not candidates:
        return RecallOutcome()
    if provider is None:
        return RecallOutcome(memories=candidates, considered=len(candidates), degraded=True)

    prompt = MEMORY_RECALL.render(
        title=title,
        service=service,
        kind=kind,
        symptoms=symptoms or "(none recorded)",
        candidates=_render_candidates(candidates),
    )
    # Memory text is human-approved, but the symptom block comes from live evidence.
    guarded = guard_input(prompt, agent="memory")

    try:
        structured = await provider.complete_structured(
            guarded.prompt,
            schema=RecallSelection,
            agent="memory",
            system=MEMORY_RECALL.system,
            prompt_ref=MEMORY_RECALL.ref,
        )
    except Exception as exc:  # noqa: BLE001 — memory is an aid, never a blocker
        _log.warning("memory_recall_unavailable", error=str(exc))
        return RecallOutcome(memories=candidates, considered=len(candidates), degraded=True)

    selection = structured.value
    # Index-based, and bounds-checked: an out-of-range index is dropped rather than wrapped,
    # so a hallucinated selection yields fewer memories instead of the wrong ones.
    chosen = [candidates[i - 1] for i in selection.selected if 1 <= i <= len(candidates)]
    _log.info(
        "memory_recall_filtered",
        considered=len(candidates),
        selected=len(chosen),
        service=service,
    )
    return RecallOutcome(
        memories=chosen,
        considered=len(candidates),
        reasoning=selection.reasoning,
        **LlmAccounting.from_result(structured.result),
    )


async def write_lesson(
    provider: LLMProvider | None,
    *,
    title: str,
    service: str,
    severity: str,
    root_cause_category: str,
    hypothesis: str,
    actions: str,
    outcome: str,
) -> MemoryLesson | None:
    """Distil a resolved incident into a lesson, or None when no model is available.

    Returning None rather than a template keeps the caller honest: `store.draft_summary`
    falls back to recording the raw facts and marks the draft as unwritten, so a human
    approving it can see that no distillation happened rather than approving assembled
    strings that read like a written summary.
    """
    if provider is None:
        return None

    prompt = MEMORY_SUMMARY.render(
        title=title,
        service=service,
        severity=severity,
        root_cause_category=root_cause_category,
        hypothesis=hypothesis,
        actions=actions,
        outcome=outcome,
    )
    guarded = guard_input(prompt, agent="memory")

    try:
        structured = await provider.complete_structured(
            guarded.prompt,
            schema=MemoryLesson,
            agent="memory",
            system=MEMORY_SUMMARY.system,
            prompt_ref=MEMORY_SUMMARY.ref,
        )
    except Exception as exc:  # noqa: BLE001 — a missing summary must not fail resolution
        _log.warning("memory_summary_unavailable", error=str(exc))
        return None

    _log.info("memory_lesson_written", service=service, confidence=structured.value.confidence)
    return structured.value
