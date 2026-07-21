"""Golden evaluation dataset (ESD §22).

Retrieval cases are written against the *shipped* runbook corpus (`eval/runbooks/`), and each
one states the query an on-call engineer would actually type — not the words the document
already contains. That distinction is the whole point: a case whose query reuses the runbook's
own vocabulary measures string matching, not retrieval.

Several cases are deliberately **adversarial**:

* wrong-service queries, which must not return a runbook for a different service;
* symptom-only queries with no shared vocabulary, which the previous hashing embedder could
  not have answered at all;
* an unanswerable query, where returning nothing is the correct behaviour and returning a
  confident irrelevant chunk is the failure.

Expected documents are identified by title substring rather than id, so the dataset survives
re-ingestion (ids are generated) without becoming order-dependent.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class RetrievalCase:
    """One retrieval expectation."""

    id: str
    query: str
    # Title substrings of documents that SHOULD rank. Empty means nothing should.
    expected_titles: tuple[str, ...] = ()
    # Title substrings that must NOT appear — the distractor this case exists to exclude.
    forbidden_titles: tuple[str, ...] = ()
    service: str | None = None
    why: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


GOLDEN_RETRIEVAL_CASES: tuple[RetrievalCase, ...] = (
    RetrievalCase(
        id="oom-plain-language",
        query="the container keeps dying because it ran out of memory",
        expected_titles=("OOMKilled",),
        why=(
            "Zero lexical overlap with the runbook, which says OOMKilled/CrashLoopBackOff. "
            "The hashing embedder scored ~0 here; this case is the reason it was replaced."
        ),
        tags=("semantic",),
    ),
    RetrievalCase(
        id="oom-exact-identifier",
        query="OOMKilled",
        expected_titles=("OOMKilled",),
        why="Rare literal token — the case lexical retrieval exists to catch.",
        tags=("lexical",),
    ),
    RetrievalCase(
        id="latency-symptom",
        query="requests are taking several seconds but nothing is erroring",
        expected_titles=("latency",),
        forbidden_titles=("OOMKilled",),
        why="Latency and crash runbooks are adjacent; picking the wrong one misdirects triage.",
        tags=("semantic", "discrimination"),
    ),
    RetrievalCase(
        id="deploy-regression",
        query="everything broke right after we shipped a release",
        expected_titles=("deploy",),
        why="Change-related phrasing that shares no words with the runbook title.",
        tags=("semantic",),
    ),
    RetrievalCase(
        id="availability",
        query="customers cannot reach the service at all",
        expected_titles=("unavailable",),
        why="Total-outage phrasing must reach the availability runbook, not the latency one.",
        tags=("semantic", "discrimination"),
    ),
    RetrievalCase(
        id="metadata-filter-catalog",
        query="pods restarting repeatedly",
        service="catalog-service",
        expected_titles=("OOMKilled",),
        why=(
            "With a service filter, every returned chunk must be tagged for that service. "
            "A runbook for a service that is not on fire is a distractor the model cannot "
            "discount on its own."
        ),
        tags=("metadata",),
    ),
    RetrievalCase(
        id="unanswerable",
        query="how do I file an expense report for the offsite",
        expected_titles=(),
        why=(
            "Nothing in an incident-response corpus answers this. Returning a confident "
            "operational runbook would be a grounding failure, not a retrieval success."
        ),
        tags=("adversarial", "out-of-domain"),
    ),
)


# --- Agent-behaviour cases (LLM-judged; see ragas_metrics) --------------------------------


@dataclass(frozen=True, slots=True)
class GroundingCase:
    """An RCA scenario with the answer a correct system would give."""

    id: str
    question: str
    contexts: tuple[str, ...]
    ground_truth: str
    why: str = ""


GOLDEN_GROUNDING_CASES: tuple[GroundingCase, ...] = (
    GroundingCase(
        id="oom-supported",
        question="Why is checkout-service failing?",
        contexts=(
            "pod checkout-service-7f9 phase=Running ready=0/1 restarts=7",
            "lastState terminated reason=OOMKilled exitCode=137",
        ),
        ground_truth=(
            "The checkout-service container is being OOMKilled and restarting, so the cause "
            "is memory exhaustion."
        ),
        why="Straightforward grounded answer; a faithful system should score high.",
    ),
    GroundingCase(
        id="deploy-unsupported",
        question="Why is checkout-service failing?",
        contexts=(
            "pod checkout-service-7f9 phase=Running ready=0/1 restarts=7",
            "github.get_recent_commits: unavailable (upstream 409)",
        ),
        ground_truth=(
            "The evidence does not identify a cause. Pods are restarting, but the deploy "
            "source was unavailable, so a code change cannot be blamed."
        ),
        why=(
            "The exact failure observed live: the model blamed 'a recent deployment' while "
            "the deploy source was down and no deploy evidence existed. 'Unknown' is correct."
        ),
    ),
    GroundingCase(
        id="healthy-not-a-fault",
        question="Is checkout-service experiencing resource exhaustion?",
        contexts=(
            "pod checkout-service-7f9 phase=Running ready=1/1 restarts=0",
            'rate(http_requests_total{status="500"}) = 0/s',
        ),
        ground_truth=(
            "No. The pod is ready with zero restarts and the error rate is zero; this "
            "evidence shows a healthy service."
        ),
        why=(
            "Mentioning restarts is not evidence OF restarts. A naive keyword check read "
            "'restarts=0' as a resource-exhaustion signal — this case guards that."
        ),
    ),
)
