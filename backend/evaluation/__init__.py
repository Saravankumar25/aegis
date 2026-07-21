"""AI quality evaluation for Aegis (ESD §22).

Every quality claim about an LLM system is either measured or asserted, and asserted claims
decay silently — a prompt edit or a model swap degrades output with no failing test. This
package makes the claims measurable.

It is split by **what each metric costs to run**, because that determines where it can live:

* ``retrieval_metrics`` — deterministic arithmetic over retrieval results (hit rate, MRR,
  precision@k, NDCG). No model, no network, no quota. These run in CI on every commit and are
  the regression gate that actually protects the RAG pipeline day to day.
* ``ragas_metrics`` — LLM-judged generation quality (faithfulness, answer relevancy, context
  precision/recall). These need a judge model, so they cost tokens and cannot gate every
  commit on a free tier. Run on demand and before a release.

RAGAS is an **optional extra** (``pip install -e ".[eval]"``), not a runtime dependency. It
pulls ~37 packages including pandas, pyarrow, datasets and langchain, and shipping that into
the production image to support a CI-only measurement would be a poor trade. The metrics
degrade to "not run" rather than failing when the extra is absent.
"""

from __future__ import annotations

from evaluation.dataset import GOLDEN_RETRIEVAL_CASES, RetrievalCase
from evaluation.retrieval_metrics import RetrievalReport, evaluate_retrieval

__all__ = [
    "GOLDEN_RETRIEVAL_CASES",
    "RetrievalCase",
    "RetrievalReport",
    "evaluate_retrieval",
]
