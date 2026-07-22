"""Repeated end-to-end incident validation against the live cluster (PRD §9A, ESD §22).

Single-run verification proves a pipeline *can* work. It says nothing about whether the same
incident produces the same quality twice, which is the question that decides whether an
autonomous system is trustworthy — an investigation that reaches the right cause once in
three runs is not a working investigation, it is a coin flip with good presentation.

This harness drives real incidents through the real stack (kind cluster, real MCP servers,
real Gemini reasoning, real Postgres) and measures *consistency* across repetitions:

* did the Observer approve, and how often
* which root-cause category was chosen, and did it vary between identical runs
* confidence spread
* citation count and whether citations were observer-validated
* which remediation the Resolution agent chose, and what it rejected
* tokens, cost and wall-clock latency per run

It deliberately reports variance rather than a pass/fail verdict. A category that flips
between runs is the finding; hiding it behind an average would defeat the purpose.

Prerequisites — this talks to real infrastructure and will fail loudly without it:
  * kind cluster up with the meridian namespace (infra/setup-cluster.sh)
  * Postgres + Redis up (docker compose up -d)
  * API on 127.0.0.1:8000 and the worker running
  * INGEST_WEBHOOK_TOKEN set in .env

Run:  python eval/scenario_validation.py --repeats 2
      python eval/scenario_validation.py --scenario pool-exhaustion --repeats 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
API_BASE = os.environ.get("AEGIS_API", "http://127.0.0.1:8000")
PROM_BASE = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
NAMESPACE = "meridian"

# How long to let an injected fault establish before alerting. Prometheus rate() needs the
# window to fill; alerting immediately produced investigations whose evidence showed a
# healthy service, which reads as an agent failure and is actually a harness failure.
SIGNAL_SETTLE_SECONDS = 90
INVESTIGATION_TIMEOUT_SECONDS = 420


@dataclass(frozen=True, slots=True)
class Scenario:
    """One reproducible fault and the alert that should follow it."""

    id: str
    service: str
    fault: str  # inject-failure.sh action
    fault_arg: str
    alert_kind: str
    alert_title: str
    alert_value: float
    # What a competent responder would conclude. Not asserted as correct — recorded so
    # variance across runs is visible and a systematic mismatch is legible.
    expected_category: str
    why: str


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="error-spike",
        service="checkout-service",
        fault="error",
        fault_arg="0.6",
        alert_kind="error_rate",
        alert_title="Checkout 5xx error rate 60% sustained",
        alert_value=0.6,
        expected_category="resource_exhaustion",
        why=(
            "Injected 5xx with logs naming upstream pool exhaustion. The dependency failure "
            "is the thread to pull; a bare 'error_spike' answer means the agent stopped at "
            "the symptom."
        ),
    ),
    Scenario(
        id="latency-degradation",
        service="catalog-service",
        fault="latency",
        fault_arg="1200",
        alert_kind="latency",
        alert_title="Catalog p99 latency above 1s",
        alert_value=1.2,
        expected_category="latency_degradation",
        why="Latency rises with a flat error rate — the discriminator against an error spike.",
    ),
    Scenario(
        id="pod-crash",
        service="payment-service",
        fault="killpod",
        fault_arg="",
        alert_kind="pod_crash",
        alert_title="Payment service pod restarting",
        alert_value=1.0,
        expected_category="resource_exhaustion",
        why="Restart/unavailability signal with no error-rate change in the application.",
    ),
    Scenario(
        id="dependency-failure",
        service="payment-service",
        fault="error",
        fault_arg="0.7",
        alert_kind="error_rate",
        alert_title="Payment service failing, checkout affected",
        alert_value=0.7,
        expected_category="resource_exhaustion",
        why=(
            "Fault at the dependency rather than the service customers notice. Tests whether "
            "the agent follows the call graph or blames the alerting service."
        ),
    ),
)

SCENARIOS_BY_ID = {s.id: s for s in SCENARIOS}


@dataclass
class RunOutcome:
    """One investigation, measured."""

    scenario_id: str
    attempt: int
    incident_id: str = ""
    final_state: str = ""
    approved: bool = False
    category: str = ""
    confidence: float | None = None
    hypothesis: str = ""
    citations: int = 0
    validated_citations: int = 0
    agents_with_explanations: list[str] = field(default_factory=list)
    action_chosen: str = ""
    alternatives_rejected: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    # The fault magnitude actually observed when the alert fired. Present in the report so a
    # reader can tell a real result from one measured against a healthy service.
    signal_at_alert: str = ""
    error: str = ""
    harness_error: bool = False


# --- infrastructure helpers ----------------------------------------------------------------


def _webhook_token() -> str:
    env = (REPO_ROOT / ".env").read_text(encoding="utf-8")
    match = re.search(r"^INGEST_WEBHOOK_TOKEN=(.+)$", env, re.M)
    if not match:
        raise SystemExit("INGEST_WEBHOOK_TOKEN is not set in .env; ingestion would 401")
    return match.group(1).strip()


class HarnessError(RuntimeError):
    """The environment could not be put into the state the scenario requires.

    Deliberately distinct from a scenario *result*. A run where the fault never took hold
    measures nothing about the agents, and scoring it as an AI failure is worse than
    discarding it — it manufactures evidence of a defect that does not exist. This is not
    hypothetical: an earlier campaign reported `category=unknown` on 8/8 runs and was read as
    a reasoning regression. The agents had been handed a healthy service and correctly
    declined to name a cause.
    """


def _kubectl(*args: str, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    """Run kubectl directly. No shell, no path translation.

    The harness originally shelled out to `infra/inject-failure.sh` via bash, which is
    unusable from a native Windows Python process: MSYS rewrites path arguments on the way
    in, so `C:\\dev\\...` arrived as `C:devAegis...` and `/c/dev/...` did not resolve either.
    Every injection failed with exit 127 — and because the call passed `check=False`, it
    failed *silently*. The campaign then alerted on perfectly healthy services and reported
    `category=unknown` on 8/8 runs, which was read as an AI reasoning regression. It was the
    harness. kubectl is a native executable invoked without a shell, so the whole class of
    quoting and translation bugs disappears.
    """
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        raise HarnessError(
            f"kubectl {' '.join(args)[:120]} exited {result.returncode}: "
            f"{(result.stderr or result.stdout)[:300]}"
        )
    return result


def _pod_ips(service: str) -> list[str]:
    result = _kubectl(
        "-n",
        NAMESPACE,
        "get",
        "pods",
        "-l",
        f"app={service}",
        "--field-selector=status.phase=Running",
        "-o",
        "jsonpath={range .items[*]}{.status.podIP}{'\\n'}{end}",
    )
    return [ip.strip() for ip in result.stdout.splitlines() if ip.strip()]


def _inject(action: str, service: str, arg: str = "") -> str:
    """Apply a fault to EVERY replica of a service, or raise.

    Per-replica by necessity: failure mode is per-process in-memory state, so addressing the
    Service load-balances to one pod and applies the fault to a fraction of traffic without
    saying so — "inject 70%" then shows up as a ~35% error rate across two replicas.
    """
    if action == "killpod":
        _kubectl(
            "-n",
            NAMESPACE,
            "delete",
            "pod",
            "-l",
            f"app={service}",
            "--grace-period=0",
            "--force",
        )
        return "killpod"

    # Branches, not a dict literal: a dict evaluates every value, so `int(arg)` would run for
    # a latency body even on an error injection where arg is "0.6" — and raise.
    if action == "error":
        body: dict[str, Any] = {"mode": "error", "rate": float(arg or 0.5)}
    elif action == "latency":
        body = {"mode": "latency", "rate": 1.0, "latency_ms": int(arg or 800)}
    elif action == "clear":
        body = {"mode": "none", "rate": 0.0}
    else:
        raise HarnessError(f"unknown fault action: {action!r}")

    ips = _pod_ips(service)
    if not ips:
        raise HarnessError(f"no running pods for app={service} in {NAMESPACE}")

    # One throwaway curl pod addressing every replica in a single invocation: a pod per
    # replica costs several seconds each, which matters across a campaign.
    urls = [f"http://{ip}:8080/admin/failure" for ip in ips]
    result = _kubectl(
        "-n",
        NAMESPACE,
        "run",
        f"aegis-inject-{uuid.uuid4().hex[:8]}",
        "--rm",
        "-i",
        "--restart=Never",
        "--image=curlimages/curl:8.10.1",
        "--",
        "-s",
        "-X",
        "POST",
        "-H",
        "Content-Type: application/json",
        "-d",
        json.dumps(body),
        *urls,
    )
    applied = result.stdout.count('"applied"')
    if applied < len(ips):
        raise HarnessError(
            f"fault '{action}' applied to {applied}/{len(ips)} replicas of {service}; "
            f"a partially-faulted service produces evidence nobody can interpret"
        )
    return result.stdout


def _prom_scalar(query: str) -> float:
    """Evaluate a PromQL query to a single number, or 0.0 when it yields nothing.

    Note the ambiguity this collapses: an unreachable Prometheus and a genuinely-zero series
    both return 0.0. That is safe *here* only because callers use it to wait for a signal to
    RISE — a persistent 0 fails the wait, which is the outcome either case deserves.
    """
    url = f"{PROM_BASE}/api/v1/query?query={urllib.parse.quote(query)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310 — local
            payload = json.load(response)
        result = payload["data"]["result"]
        return float(result[0]["value"][1]) if result else 0.0
    except (urllib.error.URLError, KeyError, ValueError, IndexError):
        return 0.0


def _prom_error_rate(service: str) -> float:
    return _prom_scalar(
        f'sum(rate(http_requests_total{{namespace="{NAMESPACE}",'
        f'pod=~"{service}.*",status="500"}}[2m]))'
    )


def _prom_p99_latency(service: str) -> float:
    """p99 request duration in seconds, over the same window the agents will query."""
    return _prom_scalar(
        f"histogram_quantile(0.99, sum by (le) (rate("
        f'http_request_duration_seconds_bucket{{namespace="{NAMESPACE}",'
        f'pod=~"{service}.*"}}[2m])))'
    )


def _prom_restarts(service: str) -> float:
    return _prom_scalar(
        f'sum(kube_pod_container_status_restarts_total{{namespace="{NAMESPACE}",'
        f'pod=~"{service}.*"}})'
    )


def observed_signal(scenario: Scenario) -> tuple[float, str]:
    """The quantity this scenario's fault should move, and a label for the report.

    Each fault type is verified against the metric an investigator would actually look at.
    Checking only the error rate — as the first version did — meant latency and pod-crash
    scenarios were never verified at all: they waited out the timer and alerted whether or
    not anything had happened.
    """
    if scenario.fault == "error":
        return _prom_error_rate(scenario.service), "5xx/s"
    if scenario.fault == "latency":
        return _prom_p99_latency(scenario.service), "p99 s"
    if scenario.fault == "killpod":
        return _prom_restarts(scenario.service), "restarts"
    return 0.0, "none"


def signal_threshold(scenario: Scenario) -> float:
    """How much movement counts as "the fault is live".

    Set well above baseline noise but well below the injected magnitude, so the wait ends as
    soon as the condition is genuinely observable rather than when it peaks.
    """
    if scenario.fault == "error":
        return 5.0  # 5xx per second; baseline is 0
    if scenario.fault == "latency":
        return 0.5  # seconds at p99; healthy is ~0.12
    if scenario.fault == "killpod":
        return 1.0  # at least one restart recorded
    return 0.0


def _post_alert(scenario: Scenario, attempt: int, token: str) -> str:
    body = json.dumps(
        {
            "alert_source": "prometheus",
            "external_alert_id": f"scenario-{scenario.id}-{attempt}-{int(time.time())}",
            "service_name": scenario.service,
            "title": scenario.alert_title,
            "kind": scenario.alert_kind,
            "value": scenario.alert_value,
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 — local API
        f"{API_BASE}/api/v1/incidents",
        data=body,
        headers={"Content-Type": "application/json", "x-aegis-webhook-token": token},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["incident_id"]


# --- measurement ----------------------------------------------------------------------------


async def _fetch_outcome(incident_id: str) -> dict:
    """Read the finished investigation straight from Postgres.

    The database rather than the API because this measures what was *persisted* — the API
    could render correctly from a response object while the durable record was incomplete,
    and the durable record is what an operator sees tomorrow.
    """
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from sqlalchemy import select

    from core.db import session_scope
    from db.models import AgentStep, EvidenceCitation, Incident, RemediationAction

    async with session_scope() as session:
        incident = await session.get(Incident, incident_id)
        steps = list(
            (
                await session.execute(
                    select(AgentStep).where(AgentStep.incident_id == incident_id)
                )
            )
            .scalars()
            .all()
        )
        citations = list(
            (
                await session.execute(
                    select(EvidenceCitation)
                    .join(AgentStep, AgentStep.id == EvidenceCitation.agent_step_id)
                    .where(AgentStep.incident_id == incident_id)
                )
            )
            .scalars()
            .all()
        )
        action = (
            (
                await session.execute(
                    select(RemediationAction)
                    .where(RemediationAction.incident_id == incident_id)
                    .order_by(RemediationAction.proposed_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

        rca = [
            s for s in steps if s.agent_name == "rca" and s.ensemble_pass_index is None
        ]
        final_rca = rca[-1] if rca else None
        output = (final_rca.structured_output or {}) if final_rca else {}
        resolution = [s for s in steps if s.agent_name == "resolution"]
        resolution_output = (
            (resolution[-1].structured_output or {}) if resolution else {}
        )

        return {
            "state": str(incident.state) if incident else "",
            "category": str(output.get("root_cause_category", "")),
            "confidence": output.get("confidence"),
            "hypothesis": str(output.get("hypothesis", ""))[:300],
            "citations": len(citations),
            "validated": sum(1 for c in citations if c.validated_by_observer),
            "explained": sorted(
                {
                    s.agent_name
                    for s in steps
                    if isinstance(s.structured_output, dict)
                    and "explanation" in s.structured_output
                }
            ),
            "action": action.action_type if action else "",
            "alternatives": len(
                resolution_output.get("alternatives_rejected", []) or []
            ),
            "tokens": sum(s.tokens_used or 0 for s in steps),
            "cost": sum(s.cost_usd or 0.0 for s in steps),
        }


async def _await_completion(incident_id: str, deadline: float) -> bool:
    """Poll persisted state until the investigation finishes or the deadline passes."""
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    from core.db import session_scope
    from db.models import Incident

    terminal = {
        "hypothesis_formed",
        "escalated",
        "remediation_proposed",
        "monitoring",
        "resolved",
    }
    while time.monotonic() < deadline:
        async with session_scope() as session:
            incident = await session.get(Incident, incident_id)
            if incident is not None and str(incident.state) in terminal:
                return True
        await asyncio.sleep(5)
    return False


async def run_scenario(scenario: Scenario, attempt: int, token: str) -> RunOutcome:
    outcome = RunOutcome(scenario_id=scenario.id, attempt=attempt)
    started = time.monotonic()
    try:
        _inject(scenario.fault, scenario.service, scenario.fault_arg)

        # Wait for the fault to become *observable*, not merely applied. The agents query
        # Prometheus over a rate window; alerting before that window has filled hands them a
        # service that still looks healthy and makes a correct "unknown" look like a failure.
        threshold = signal_threshold(scenario)
        settle_deadline = time.monotonic() + SIGNAL_SETTLE_SECONDS
        level, unit = observed_signal(scenario)
        while level < threshold and time.monotonic() < settle_deadline:
            await asyncio.sleep(10)
            level, unit = observed_signal(scenario)

        if level < threshold:
            # Refuse to alert. A benchmark that cannot distinguish "the fault was live" from
            # "the fault never landed" produces numbers that mean nothing, and the failure
            # mode is silent: the agents get blamed for the environment.
            raise HarnessError(
                f"fault never became observable — {unit} reached {level:.2f}, "
                f"needed {threshold:.2f} within {SIGNAL_SETTLE_SECONDS}s. Not alerting; "
                f"this run measures the harness, not the agents."
            )
        outcome.signal_at_alert = f"{level:.2f} {unit}"

        outcome.incident_id = _post_alert(scenario, attempt, token)
        finished = await _await_completion(
            outcome.incident_id, time.monotonic() + INVESTIGATION_TIMEOUT_SECONDS
        )
        if not finished:
            outcome.error = (
                "investigation did not reach a terminal state before the timeout"
            )
            return outcome

        measured = await _fetch_outcome(outcome.incident_id)
        outcome.final_state = measured["state"]
        outcome.approved = measured["state"] != "escalated"
        outcome.category = measured["category"]
        outcome.confidence = measured["confidence"]
        outcome.hypothesis = measured["hypothesis"]
        outcome.citations = measured["citations"]
        outcome.validated_citations = measured["validated"]
        outcome.agents_with_explanations = measured["explained"]
        outcome.action_chosen = measured["action"]
        outcome.alternatives_rejected = measured["alternatives"]
        outcome.tokens = measured["tokens"]
        outcome.cost_usd = measured["cost"]
    except HarnessError as exc:
        # Not an AI result. Reported separately so it can never be averaged into quality.
        outcome.error = str(exc)
        outcome.harness_error = True
    except Exception as exc:  # noqa: BLE001 — one bad run must not abort the campaign
        outcome.error = f"{type(exc).__name__}: {exc}"
    finally:
        # Cleanup must not raise. A failure here would replace whatever the run actually
        # found with a teardown error, and leaving the fault applied would poison every
        # subsequent scenario — so it is recorded rather than propagated.
        try:
            _inject("clear", scenario.service)
        except HarnessError as exc:
            outcome.error = (
                outcome.error + " | " if outcome.error else ""
            ) + f"cleanup: {exc}"
            outcome.harness_error = True
        outcome.wall_seconds = round(time.monotonic() - started, 1)
    return outcome


# --- reporting -------------------------------------------------------------------------------


def format_report(outcomes: list[RunOutcome]) -> str:
    lines = [
        "",
        "=" * 100,
        "SCENARIO VALIDATION — consistency across repeated runs",
        "=" * 100,
    ]

    by_scenario: dict[str, list[RunOutcome]] = {}
    for outcome in outcomes:
        by_scenario.setdefault(outcome.scenario_id, []).append(outcome)

    for scenario_id, runs in by_scenario.items():
        scenario = SCENARIOS_BY_ID[scenario_id]
        lines += [
            "",
            f"--- {scenario_id} ({scenario.service}) ---",
            f"    {scenario.why}",
        ]
        lines.append(
            f"    {'run':>4} {'state':<20} {'category':<22} {'conf':>5} "
            f"{'cites':>6} {'action':<18} {'tok':>7} {'sec':>6}"
        )
        for run in runs:
            if run.error:
                lines.append(f"    {run.attempt:>4} ERROR: {run.error[:80]}")
                continue
            conf = f"{run.confidence:.2f}" if run.confidence is not None else "  - "
            lines.append(
                f"    {run.attempt:>4} {run.final_state:<20} {run.category:<22} {conf:>5} "
                f"{run.validated_citations}/{run.citations:<4} {run.action_chosen or '-':<18} "
                f"{run.tokens:>7} {run.wall_seconds:>6.0f}  signal={run.signal_at_alert or '-'}"
            )

        clean = [r for r in runs if not r.error]  # harness failures carry .error too
        if len(clean) > 1:
            categories = {r.category for r in clean}
            confidences = [r.confidence for r in clean if r.confidence is not None]
            approvals = sum(1 for r in clean if r.approved)
            stability = (
                "STABLE" if len(categories) == 1 else f"VARIES {sorted(categories)}"
            )
            lines.append(
                f"    consistency: category={stability}"
                f"  approved={approvals}/{len(clean)}"
                + (
                    f"  confidence spread={max(confidences) - min(confidences):.2f}"
                    if len(confidences) > 1
                    else ""
                )
            )
            if len(confidences) > 1:
                lines.append(
                    f"    confidence: mean={statistics.mean(confidences):.2f} "
                    f"stdev={statistics.pstdev(confidences):.2f}"
                )
            explained = {tuple(r.agents_with_explanations) for r in clean}
            lines.append(
                f"    explanations: {'consistent' if len(explained) == 1 else 'VARIES'} "
                f"{sorted(clean[0].agents_with_explanations)}"
            )

    clean_all = [r for r in outcomes if not r.error]
    if clean_all:
        lines += [
            "",
            "-" * 100,
            f"TOTAL runs={len(outcomes)} clean={len(clean_all)} "
            f"errors={len(outcomes) - len(clean_all)}",
            f"approved={sum(1 for r in clean_all if r.approved)}/{len(clean_all)}  "
            f"mean_tokens={statistics.mean([r.tokens for r in clean_all]):.0f}  "
            f"mean_seconds={statistics.mean([r.wall_seconds for r in clean_all]):.0f}  "
            f"total_cost=${sum(r.cost_usd for r in clean_all):.4f}",
        ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=2, help="runs per scenario")
    parser.add_argument("--scenario", help="run only this scenario id")
    args = parser.parse_args()

    chosen = [SCENARIOS_BY_ID[args.scenario]] if args.scenario else list(SCENARIOS)
    token = _webhook_token()

    outcomes: list[RunOutcome] = []
    for scenario in chosen:
        for attempt in range(1, args.repeats + 1):
            print(f"[{scenario.id}] run {attempt}/{args.repeats} ...", flush=True)
            outcome = await run_scenario(scenario, attempt, token)
            outcomes.append(outcome)
            print(
                f"    -> {outcome.final_state or 'ERROR'} "
                f"{outcome.category} ({outcome.wall_seconds}s)",
                flush=True,
            )
    print(format_report(outcomes))


if __name__ == "__main__":
    asyncio.run(main())
