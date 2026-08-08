"""Unit tests: RAGAS adapter and report logic (ESD §22).

These test the plumbing around RAGAS — dataset shaping, degradation when unavailable, the
per-case score attachment — with doubles, never a real judge model. A judge call costs real
tokens and needs `GEMINI_API_KEYS`; per-commit tests must not depend on either. The one test
that calls a live judge is marked `ragas_live` and is opt-in (see `test_ragas_live.py`).

The property worth guarding most carefully: **absence must degrade to `available=False`,
never to an import error or a fabricated score.** `tests/unit/test_no_fabrication.py`
enforces the project-wide version of this; the tests here are the RAGAS-specific instance —
a judge failure must be legible as "not measured", never mistaken for "measured as good".
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from evaluation.ragas_metrics import (
    GenerationReport,
    build_dataset,
    evaluate_generation,
    format_report,
    ragas_available,
)


@dataclass(frozen=True, slots=True)
class _Case:
    id: str
    question: str
    contexts: tuple[str, ...]
    ground_truth: str
    why: str = ""
    expects_refusal: bool = False


CASES = (
    _Case(
        id="grounded",
        question="Why is checkout-service failing?",
        contexts=("OOMKilled", "restarts=7"),
        ground_truth="Memory exhaustion caused the crash loop.",
        why="A faithful system should score high here.",
    ),
    _Case(
        id="unsupported",
        question="Why is checkout-service failing?",
        contexts=("github: unavailable",),
        ground_truth="The evidence does not establish a cause.",
    ),
)


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_ragas_available_reports_absence_without_raising():
    """Must never raise ImportError at call time — only report it."""
    available, reason = ragas_available()
    assert isinstance(available, bool)
    assert isinstance(reason, str)


def test_build_dataset_shapes_records_for_ragas():
    records = build_dataset(CASES)
    assert len(records) == len(CASES)
    for case, record in zip(CASES, records, strict=True):
        assert record["user_input"] == case.question
        assert record["retrieved_contexts"] == list(case.contexts)
        assert record["reference"] == case.ground_truth
        # No `response` yet — that is added by the caller once an answer exists.
        assert "response" not in record


async def test_no_judge_degrades_without_raising():
    """A caller that forgets to configure a judge gets a legible reason, not a crash — and
    not a score, which would be indistinguishable from a real measurement.

    The invariant asserted unconditionally is the one that matters: no score, no exception.
    Which *reason* is reported depends on the environment — CI installs the runtime deps only,
    so it legitimately stops at "ragas not installed" before it can reach the judge check.
    Asserting the judge wording unconditionally made this test pass only on a machine that
    happened to have the optional extra.
    """
    report = await evaluate_generation(CASES, answers=["a", "b"], judge_llm=None)
    assert report.available is False
    assert report.scores == {}
    assert report.reason  # never silently blank — absence must always be legible

    ok, _ = ragas_available()
    if ok:
        assert "judge" in report.reason.lower()
    else:
        assert "ragas" in report.reason.lower()


async def test_mismatched_answer_count_raises_rather_than_misattributing():
    """One answer short would silently score every later case against the wrong question.

    Asserted with no judge configured, which is the environment CI actually runs in: the
    argument check has to fire before any optional-dependency or judge check, or a caller bug
    degrades into a quiet `available=False` instead of an error.
    """
    with pytest.raises(ValueError, match="answers"):
        await evaluate_generation(CASES, answers=["only one"], judge_llm=None)


def test_report_summary_when_unavailable_names_the_reason():
    report = GenerationReport(available=False, reason="no GEMINI_API_KEYS configured")
    assert "no GEMINI_API_KEYS configured" in report.summary()


def test_report_summary_when_available_lists_every_score():
    report = GenerationReport(available=True, scores={"faithfulness": 0.9, "answer_relevancy": 0.5})
    summary = report.summary()
    assert "faithfulness=0.90" in summary
    assert "answer_relevancy=0.50" in summary


def test_format_report_handles_unavailable_report():
    report = GenerationReport(available=False, reason="ragas not installed")
    text = format_report(report, cases=CASES)
    assert "NOT RUN" in text
    assert "ragas not installed" in text


def test_format_report_surfaces_per_case_scores_not_just_the_mean():
    """A mean can hide the one case that matters — `unsupported` exists specifically to
    catch a model that fabricates a cause, and that must be readable per-case."""
    report = GenerationReport(
        available=True,
        scores={"faithfulness": 0.5},
        per_case=[
            {"response": "grounded answer", "faithfulness": 1.0},
            {"response": "fabricated answer", "faithfulness": 0.0},
        ],
    )
    text = format_report(report, cases=CASES)
    assert "faithfulness=1.00" in text
    assert "faithfulness=0.00" in text


async def test_relevancy_metric_is_skipped_without_embeddings_not_the_whole_run(monkeypatch):
    """Faithfulness and context precision need only the judge LLM; ResponseRelevancy alone
    needs embeddings. Missing embeddings must narrow the metric set, not fail the run.

    Patches the real `ragas.evaluate` to capture what it was actually called with, rather
    than mocking `evaluate_generation` itself — the point is to prove *this module's* metric
    selection, not to assert a fake result.
    """
    ok, _ = ragas_available()
    if not ok:
        pytest.skip("ragas not installed in this environment")

    import ragas

    captured: dict = {}

    class _FakeResult:
        _repr_dict = {"faithfulness": 1.0}

        def to_pandas(self):
            import pandas as pd

            return pd.DataFrame([{"faithfulness": 1.0}, {"faithfulness": 1.0}])

    def _fake_evaluate(*, dataset, metrics, llm, embeddings=None, **_):
        captured["metric_names"] = [type(m).__name__ for m in metrics]
        captured["embeddings"] = embeddings
        return _FakeResult()

    monkeypatch.setattr(ragas, "evaluate", _fake_evaluate)

    report = await evaluate_generation(
        CASES, answers=["a", "b"], judge_llm=object(), judge_embeddings=None
    )
    assert report.available is True
    assert "ResponseRelevancy" not in captured["metric_names"]
    assert captured["embeddings"] is None


async def test_relevancy_metric_is_included_when_embeddings_are_given(monkeypatch):
    ok, _ = ragas_available()
    if not ok:
        pytest.skip("ragas not installed in this environment")

    import ragas

    captured: dict = {}

    class _FakeResult:
        _repr_dict = {"faithfulness": 1.0, "answer_relevancy": 0.5}

        def to_pandas(self):
            import pandas as pd

            return pd.DataFrame(
                [
                    {"faithfulness": 1.0, "answer_relevancy": 0.5},
                    {"faithfulness": 1.0, "answer_relevancy": 0.5},
                ]
            )

    def _fake_evaluate(*, dataset, metrics, llm, embeddings=None, **_):
        captured["metric_names"] = [type(m).__name__ for m in metrics]
        return _FakeResult()

    monkeypatch.setattr(ragas, "evaluate", _fake_evaluate)

    report = await evaluate_generation(
        CASES, answers=["a", "b"], judge_llm=object(), judge_embeddings=object()
    )
    assert report.available is True
    assert "ResponseRelevancy" in captured["metric_names"]


async def test_per_case_scores_are_attached_to_each_record(monkeypatch):
    """The aggregate is not the whole story — a per-case regression must be visible."""
    ok, _ = ragas_available()
    if not ok:
        pytest.skip("ragas not installed in this environment")

    import ragas

    class _FakeResult:
        _repr_dict = {"faithfulness": 0.5}

        def to_pandas(self):
            import pandas as pd

            return pd.DataFrame([{"faithfulness": 1.0}, {"faithfulness": 0.0}])

    monkeypatch.setattr(
        ragas, "evaluate", lambda *, dataset, metrics, llm, embeddings=None, **_: _FakeResult()
    )

    report = await evaluate_generation(CASES, answers=["a", "b"], judge_llm=object())
    assert report.per_case[0]["faithfulness"] == 1.0
    assert report.per_case[1]["faithfulness"] == 0.0


def test_relevancy_aggregate_excludes_refusal_by_design_cases():
    """The reporting decision that stops a correct refusal reading as a quality failure.

    RAGAS scores a noncommittal answer 0 by design. Aegis's two most important golden cases
    are ones where refusing IS the correct answer, so the raw mean reported 0.33 relevancy
    for a system that was behaving exactly as intended. The report must show both the raw
    aggregate and one computed over cases where the metric is applicable.
    """
    cases = (
        _Case(id="answerable", question="q", contexts=("c",), ground_truth="a"),
        _Case(
            id="refusal",
            question="q",
            contexts=("c",),
            ground_truth="evidence does not establish a cause",
            expects_refusal=True,
        ),
    )
    report = GenerationReport(
        available=True,
        scores={"answer_relevancy": 0.5},
        per_case=[
            {"response": "a real answer", "answer_relevancy": 1.0},
            {"response": "the evidence does not establish", "answer_relevancy": 0.0},
        ],
    )
    text = format_report(report, cases=cases)
    assert "over answerable cases only = 1.00" in text
    assert "refusal" in text
    # The raw aggregate is still shown — neither number is hidden from the reader.
    assert "answer_relevancy=0.50" in text


def test_relevancy_exclusion_is_silent_when_no_refusal_cases_exist():
    """No special-casing noise on a dataset where every case is answerable."""
    cases = (_Case(id="a", question="q", contexts=("c",), ground_truth="x"),)
    report = GenerationReport(
        available=True,
        scores={"answer_relevancy": 0.9},
        per_case=[{"response": "answer", "answer_relevancy": 0.9}],
    )
    text = format_report(report, cases=cases)
    assert "over answerable cases only" not in text
