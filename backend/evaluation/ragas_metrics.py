"""RAGAS-backed generation quality metrics (ESD §22).

These are the metrics that need a **judge model**: faithfulness (is every claim entailed by
the retrieved context), answer relevancy (does the answer address the question), and context
precision/recall (was the right context retrieved, and was it used). They cannot be computed
arithmetically, which is why they are separated from `retrieval_metrics`.

Two consequences follow, and both are deliberate:

**RAGAS is an optional extra**, installed with ``pip install -e ".[eval]"``. It pulls ~37
packages — pandas, pyarrow, datasets, langchain, openai — and shipping that into the
production runtime image to support a CI-only measurement is a poor trade. Absence degrades
to `available=False`, never to an import error at startup.

**These run on demand, not per commit.** Each case costs judge-model tokens. On a free tier
that makes them unusable as a per-commit gate, so the per-commit gate is the deterministic
retrieval suite and this is the pre-release check.

The faithfulness metric is the one that matters most here. Aegis's central claim is that its
hypotheses are grounded in cited evidence; faithfulness is the only metric that measures that
claim directly rather than measuring a proxy for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.logging import get_logger

_log = get_logger(component="evaluation.ragas")


@dataclass(slots=True)
class GenerationReport:
    """Scores from an LLM-judged evaluation run."""

    available: bool
    reason: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    per_case: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        if not self.available:
            return f"generation metrics not run: {self.reason}"
        return " ".join(f"{k}={v:.2f}" for k, v in sorted(self.scores.items()))


def ragas_available() -> tuple[bool, str]:
    """Whether the optional eval extra is installed."""
    try:
        import ragas  # noqa: F401
    except ImportError as exc:
        return False, f"ragas not installed ({exc}); pip install -e '.[eval]'"
    return True, ""


def build_dataset(cases) -> list[dict[str, Any]]:
    """Shape golden cases into RAGAS's expected record format.

    Kept separate from `evaluate` so the dataset can be inspected, serialised, or fed to a
    different harness without importing RAGAS at all.
    """
    return [
        {
            "user_input": case.question,
            "retrieved_contexts": list(case.contexts),
            "reference": case.ground_truth,
        }
        for case in cases
    ]


async def evaluate_generation(
    cases,
    *,
    answers: list[str],
    judge_llm: Any | None = None,
) -> GenerationReport:
    """Score generated answers against golden cases.

    ``answers`` are produced by the real pipeline and passed in rather than generated here:
    an evaluator that also generates the thing it is grading tests the evaluator, not the
    system. The caller runs the agents; this only judges the output.
    """
    ok, reason = ragas_available()
    if not ok:
        _log.info("ragas_unavailable", reason=reason)
        return GenerationReport(available=False, reason=reason)

    if judge_llm is None:
        return GenerationReport(
            available=False,
            reason=(
                "no judge model configured. RAGAS needs an LLM to score faithfulness and "
                "relevancy; set one explicitly rather than defaulting, so evaluation cost is "
                "never incurred by accident."
            ),
        )

    if len(answers) != len(cases):
        raise ValueError(
            f"got {len(answers)} answers for {len(cases)} cases — they must correspond, "
            f"or every score is attributed to the wrong case"
        )

    try:
        from ragas import EvaluationDataset, evaluate
        from ragas.metrics import (
            Faithfulness,
            LLMContextPrecisionWithReference,
            ResponseRelevancy,
        )

        records = build_dataset(cases)
        for record, answer in zip(records, answers, strict=True):
            record["response"] = answer

        dataset = EvaluationDataset.from_list(records)
        result = evaluate(
            dataset=dataset,
            metrics=[
                Faithfulness(),  # the grounding claim, measured directly
                ResponseRelevancy(),
                LLMContextPrecisionWithReference(),
            ],
            llm=judge_llm,
        )
        scores = {k: float(v) for k, v in result._repr_dict.items() if isinstance(v, int | float)}
        return GenerationReport(available=True, scores=scores, per_case=records)
    except Exception as exc:  # noqa: BLE001 — an eval failure must not read as a quality pass
        _log.warning("ragas_evaluation_failed", error=str(exc))
        return GenerationReport(
            available=False, reason=f"ragas evaluation failed: {type(exc).__name__}: {exc}"
        )
