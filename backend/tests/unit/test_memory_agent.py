"""Unit tests: Memory agent relevance judgement and lesson writing (FR-7).

The safety property here is one-directional and easy to lose in a refactor: the LLM may
only ever **narrow** the set of memories that reach an investigation. The
`approved_by IS NOT NULL` filter lives in SQL (`memory.store.recall`), so the model never
sees an unapproved memory and therefore cannot surface one — but a future change that let
the model name memories by id instead of by index would quietly hand it that power. The
index-selection tests below are what make that regression visible.
"""

from __future__ import annotations

import uuid

import pytest

from db.models import MemorySummary
from memory.agent import MemoryLesson, RecallSelection, select_relevant, write_lesson
from providers.base import LLMResult, StructuredResult

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeProvider:
    """Returns a scripted structured value. Not a runtime provider (CLAUDE.md §18)."""

    name = "fake"

    def __init__(self, value=None, raises: Exception | None = None):
        self._value = value
        self._raises = raises

    async def complete_structured(self, prompt, *, schema, agent, system=None, **kw):
        if self._raises:
            raise self._raises
        return StructuredResult(
            value=self._value,
            result=LLMResult(
                text="{}",
                model="fake-model",
                tokens_used=50,
                cost_usd=0.0,
                latency_ms=20,
                prompt_ref=kw.get("prompt_ref"),
            ),
        )


def _memory(symptom: str, cause: str = "cause", type_: str = "resource_exhaustion"):
    return MemorySummary(
        id=uuid.uuid4(),
        incident_id=uuid.uuid4(),
        service_name="checkout-service",
        incident_type=type_,
        symptom=symptom,
        root_cause=cause,
        fix="restarted",
        outcome="resolved",
        approved_by=uuid.uuid4(),
    )


async def _select(provider, candidates):
    return await select_relevant(
        provider,
        candidates,
        title="checkout 5xx spike",
        service="checkout-service",
        kind="error_rate",
        symptoms="pool exhausted",
    )


# --- narrowing, never widening -------------------------------------------------------------


async def test_only_selected_candidates_are_returned():
    candidates = [_memory("a"), _memory("b"), _memory("c")]
    outcome = await _select(
        FakeProvider(RecallSelection(selected=[2], reasoning="matches")), candidates
    )
    assert [m.symptom for m in outcome.memories] == ["b"]
    assert outcome.considered == 3


async def test_empty_selection_is_valid_and_returns_nothing():
    """Most past incidents on a service are unrelated; returning none is the right answer."""
    candidates = [_memory("a"), _memory("b")]
    outcome = await _select(FakeProvider(RecallSelection(selected=[])), candidates)
    assert outcome.memories == []
    assert outcome.considered == 2


@pytest.mark.parametrize("bad", [[0], [4], [-1], [99], [0, 4]])
async def test_out_of_range_indices_are_dropped_not_wrapped(bad):
    """A hallucinated index must yield fewer memories, never a different one.

    Negative indices are the sharp edge: Python would happily resolve -1 to the last
    candidate, silently substituting a memory the model did not choose.
    """
    candidates = [_memory("a"), _memory("b"), _memory("c")]
    outcome = await _select(FakeProvider(RecallSelection(selected=bad)), candidates)
    assert outcome.memories == []


async def test_selection_cannot_return_more_than_it_was_given():
    candidates = [_memory("a"), _memory("b")]
    outcome = await _select(FakeProvider(RecallSelection(selected=[1, 1, 2, 2])), candidates)
    assert len(outcome.memories) <= len(candidates) * 2  # duplicates possible, new items not
    assert all(m in candidates for m in outcome.memories)


# --- degradation ----------------------------------------------------------------------------


async def test_unavailable_model_falls_back_to_all_approved_candidates():
    """Degrades toward the previous behaviour, not toward silence.

    Every candidate is already human-approved for this service, so returning them unfiltered
    is the pre-existing recency behaviour. Returning nothing would drop institutional
    knowledge during exactly the outage it was written for.
    """
    candidates = [_memory("a"), _memory("b")]
    outcome = await _select(FakeProvider(raises=RuntimeError("down")), candidates)
    assert outcome.memories == candidates
    assert outcome.degraded is True


async def test_no_provider_falls_back_to_candidates():
    candidates = [_memory("a")]
    outcome = await _select(None, candidates)
    assert outcome.memories == candidates
    assert outcome.degraded is True


async def test_no_candidates_short_circuits_without_calling_the_model():
    outcome = await _select(FakeProvider(raises=AssertionError("must not be called")), [])
    assert outcome.memories == []
    assert outcome.considered == 0


# --- lesson writing --------------------------------------------------------------------------


async def _write(provider):
    return await write_lesson(
        provider,
        title="High 5xx on checkout",
        service="checkout-service",
        severity="P1",
        root_cause_category="resource_exhaustion",
        hypothesis="connection pool exhausted",
        actions="restart_pod",
        outcome="resolved",
    )


async def test_lesson_is_written_by_the_model():
    lesson = MemoryLesson(
        symptom="checkout returns 500s and payment calls time out",
        root_cause="connection pool exhausted",
        fix="raised pool size and restarted",
        outcome="resolved",
        confidence=0.8,
    )
    written = await _write(FakeProvider(lesson))
    assert written is not None
    assert written.symptom.startswith("checkout returns")


async def test_unavailable_model_returns_none_rather_than_a_template():
    """The caller records raw facts and says so, instead of emitting assembled strings that
    read like a written summary — a human approving it must be able to tell the difference."""
    assert await _write(FakeProvider(raises=RuntimeError("down"))) is None
    assert await _write(None) is None
