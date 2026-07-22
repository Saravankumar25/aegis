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
    judge_embeddings: Any | None = None,
) -> GenerationReport:
    """Score generated answers against golden cases.

    ``answers`` are produced by the real pipeline and passed in rather than generated here:
    an evaluator that also generates the thing it is grading tests the evaluator, not the
    system. The caller runs the agents; this only judges the output.

    ``judge_embeddings`` is optional and only needed for ``ResponseRelevancy``, which compares
    a question regenerated from the answer against the original in embedding space. Without
    it, that one metric is skipped rather than the whole run failing — faithfulness and
    context precision need only the judge LLM.
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

        # Pinned to `ragas.metrics` even though it emits a DeprecationWarning pointing at
        # `ragas.metrics.collections`. The collections classes take their dependencies at
        # construction (which is the better design) but are NOT accepted by ragas's own
        # `evaluate()` runner in 0.4.3 — it rejects them with "All metrics must be
        # initialised metric objects". Verified, not assumed. Migrating means either waiting
        # for a compatible runner or hand-rolling the execution loop, and a deprecation
        # warning is not breakage whereas a non-functional evaluation is.
        from ragas.metrics import (
            Faithfulness,
            LLMContextPrecisionWithReference,
            ResponseRelevancy,
        )

        records = build_dataset(cases)
        for record, answer in zip(records, answers, strict=True):
            record["response"] = answer

        metrics: list[Any] = [
            Faithfulness(),  # the grounding claim, measured directly
            LLMContextPrecisionWithReference(),
        ]
        if judge_embeddings is not None:
            metrics.append(ResponseRelevancy())
        else:
            _log.info("ragas_relevancy_skipped", reason="no judge_embeddings configured")

        dataset = EvaluationDataset.from_list(records)
        result = evaluate(
            dataset=dataset, metrics=metrics, llm=judge_llm, embeddings=judge_embeddings
        )
        scores = {k: float(v) for k, v in result._repr_dict.items() if isinstance(v, int | float)}

        # Per-case scores, not just the aggregate. An average hides exactly the case that
        # matters most here — `deploy-unsupported` and `healthy-not-a-fault` exist to catch
        # a model that fabricates a cause, and that failure mode would still show a
        # respectable *mean* faithfulness if only one of three cases regressed.
        metric_names = list(scores)
        per_case_df = result.to_pandas()
        for i, record in enumerate(records):
            for name in metric_names:
                if name in per_case_df.columns:
                    record[name] = float(per_case_df.iloc[i][name])

        return GenerationReport(available=True, scores=scores, per_case=records)
    except Exception as exc:  # noqa: BLE001 — an eval failure must not read as a quality pass
        _log.warning("ragas_evaluation_failed", error=str(exc))
        return GenerationReport(
            available=False, reason=f"ragas evaluation failed: {type(exc).__name__}: {exc}"
        )


_ANSWER_SYSTEM = (
    "You are an SRE answering strictly from the evidence given. Do not use outside "
    "knowledge and do not speculate beyond what the evidence states. If the evidence does "
    "not establish a cause, say plainly that it does not — that is a correct answer, not a "
    "failure to answer."
)


async def _generate_answer(provider: Any, case: Any) -> str:
    """Produce a real answer for one grounding case from the real generation model.

    Deliberately a plain `complete()` call rather than the actual RCA ensemble: this module
    evaluates the *judging* pipeline (faithfulness, relevancy, context precision) against a
    fixed, inspectable prompt, not the full agent. Running the real multi-pass RCA ensemble
    for three golden cases would also cost several times the tokens for no additional signal
    about whether the judge itself is trustworthy.
    """
    context_block = "\n".join(f"- {c}" for c in case.contexts)
    prompt = f"Evidence:\n{context_block}\n\nQuestion: {case.question}"
    result = await provider.complete(prompt, agent="rca", system=_ANSWER_SYSTEM, max_tokens=300)
    return result.text.strip()


async def run_golden_grounding_eval(cases=None) -> GenerationReport:
    """Generate real answers and judge them with a real model. The pre-release RAG check.

    Wires `evaluation.judge` (Gemini via its OpenAI-compatible surface) as both the answer
    generator and the judge. Using the same vendor for both is fine here because generation
    and judging are structurally different tasks with different prompts — the judge is not
    grading its own unconstrained output, it is checking a *constrained* answer against
    *evidence both the generator and judge can see*, which is what faithfulness measures.
    """
    from evaluation.dataset import GOLDEN_GROUNDING_CASES
    from evaluation.judge import JudgeUnavailable, build_judge_embeddings, build_judge_llm
    from providers.factory import get_provider

    cases = cases or GOLDEN_GROUNDING_CASES
    try:
        judge_llm = build_judge_llm()
    except JudgeUnavailable as exc:
        return GenerationReport(available=False, reason=str(exc))

    try:
        judge_embeddings = build_judge_embeddings()
    except Exception as exc:  # noqa: BLE001 — relevancy is optional, not the whole run
        _log.warning("ragas_embeddings_unavailable", error=str(exc))
        judge_embeddings = None

    provider = get_provider()
    answers = [await _generate_answer(provider, case) for case in cases]

    return await evaluate_generation(
        cases, answers=answers, judge_llm=judge_llm, judge_embeddings=judge_embeddings
    )


def format_report(report: GenerationReport, cases=None) -> str:
    from evaluation.dataset import GOLDEN_GROUNDING_CASES

    cases = cases or GOLDEN_GROUNDING_CASES
    lines = [
        "",
        "=" * 78,
        "RAGAS generation quality — LLM-judged (on-demand, not per-commit)",
        "=" * 78,
    ]
    if not report.available:
        lines.append(f"NOT RUN: {report.reason}")
        return "\n".join(lines)

    lines.append(report.summary())

    # Relevancy, recomputed over only the cases where it means anything. RAGAS scores a
    # noncommittal answer 0 by design, so a case whose *correct* answer is a refusal drags
    # the aggregate down for behaving correctly. Reporting the raw mean would present the
    # system's most important safety property — declining to invent a cause — as a quality
    # failure. Both numbers are shown so neither is hidden.
    relevancy_key = next((k for k in report.scores if "relevancy" in k), None)
    if relevancy_key:
        scored = [
            (case, rec)
            for case, rec in zip(cases, report.per_case, strict=False)
            if relevancy_key in rec
        ]
        answerable = [rec[relevancy_key] for case, rec in scored if not case.expects_refusal]
        refusals = [case.id for case, _ in scored if case.expects_refusal]
        if answerable and refusals:
            mean = sum(answerable) / len(answerable)
            lines.append(
                f"{relevancy_key} over answerable cases only = {mean:.2f} "
                f"(excluded {len(refusals)} refusal-by-design case(s): {', '.join(refusals)} — "
                f"RAGAS scores a correct refusal 0)"
            )

    lines.append("")
    metric_names = list(report.scores)
    for case, record in zip(cases, report.per_case, strict=False):
        marker = "  [refusal expected]" if case.expects_refusal else ""
        lines.append(f"[{case.id}]{marker} {case.why or case.question}")
        per_metric = ", ".join(
            f"{name}={record[name]:.2f}" for name in metric_names if name in record
        )
        if per_metric:
            lines.append(f"  scores: {per_metric}")
        lines.append(f"  answer: {record.get('response', '')[:200]}")
    return "\n".join(lines)


async def main() -> None:
    report = await run_golden_grounding_eval()
    print(format_report(report))


if __name__ == "__main__":
    import asyncio as _asyncio

    _asyncio.run(main())
