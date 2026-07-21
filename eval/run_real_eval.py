"""Score the synthetic incident corpus against a REAL LLM (PRD 9A).

This is THE accuracy measurement: it runs the corpus through the real configured
model and reports the two numbers that decide whether the system is trustworthy.
There is no offline fallback — if capacity is exhausted the run fails loudly rather
than reporting numbers that did not come from a real model.

- **RCA accuracy** — category vs. hand-labelled ground truth (target ≥85%).
- **Hallucination rate** — share of claims whose citation does not resolve to a piece of
  evidence that was actually gathered (target <5%). This is the number that matters: a
  model inventing an evidence id is the failure mode the Observer exists to catch, so the
  script reports both the raw rate and what the Observer did about it.

Run from the repo root:  backend/.venv/Scripts/python.exe eval/run_real_eval.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

from agents.correlation.collector import collect_evidence  # noqa: E402
from tests.support.doubles import ReplayGateway  # noqa: E402
from agents.observer.validator import review, validate_claims  # noqa: E402
from agents.rca.engine import run_rca  # noqa: E402
from core.config import get_settings  # noqa: E402
from providers.factory import get_provider  # noqa: E402

SCENARIOS = sorted((REPO / "eval" / "synthetic_incidents").glob("*.json"))


async def run_one(provider, scenario: dict) -> dict:
    fixtures = {tuple(k.split("::", 1)): v for k, v in scenario["fixtures"].items()}
    store, _ = await collect_evidence(ReplayGateway(fixtures), scenario["service_name"])

    started = time.perf_counter()
    result = await run_rca(
        provider,
        service=scenario["service_name"],
        title=scenario["title"],
        store=store,
        runbook_context="",
    )
    verdict = review(result.claims, store, root_cause_category=result.root_cause_category)

    # Mirror the orchestrator's single bounded revision: if the Observer rejected
    # because evidence looked like injected instructions, drop it and re-reason.
    revised = False
    if not verdict.approved and verdict.flagged_evidence:
        poisoned = {f["evidence_id"] for f in verdict.flagged_evidence}
        store.items = [i for i in store.items if i.id not in poisoned]
        result = await run_rca(
            provider,
            service=scenario["service_name"],
            title=scenario["title"],
            store=store,
            runbook_context="",
        )
        verdict = review(
            result.claims, store, root_cause_category=result.root_cause_category
        )
        revised = True

    claim_verdicts = validate_claims(result.claims, store)
    return {
        "name": scenario["name"],
        "expected": scenario["ground_truth_category"],
        "got": result.root_cause_category,
        "correct": result.root_cause_category == scenario["ground_truth_category"],
        "claims": len(claim_verdicts),
        "hallucinated": sum(1 for v in claim_verdicts if not v.valid),
        "agreement": result.agreement_score,
        "ensemble_degraded": result.ensemble_degraded,
        "passes_ok": f"{result.passes_succeeded}/{result.passes_requested}",
        "category_supported": verdict.category_supported,
        "models": ",".join(result.models_used),
        "low_confidence": result.low_confidence,
        "observer_approved": verdict.approved,
        "revised": revised,
        "passes": len(result.passes),
        "tokens": result.tokens_used,
        "seconds": round(time.perf_counter() - started, 1),
    }


async def main() -> int:
    settings = get_settings()
    provider = get_provider()
    print(f"provider={settings.llm_provider}  rca_model={settings.llm_model_rca}")
    print(f"scenarios={len(SCENARIOS)}  ensemble_passes={settings.rca_ensemble_passes}\n")

    rows = []
    for path in SCENARIOS:
        scenario = json.loads(path.read_text(encoding="utf-8"))
        row = await run_one(provider, scenario)
        rows.append(row)
        mark = "PASS" if row["correct"] else "MISS"
        halluc = "" if row["hallucinated"] == 0 else f"  HALLUCINATED={row['hallucinated']}"
        print(
            f"[{mark}] {row['name']:<34} got={row['got']:<22} "
            f"agree={row['agreement']:.2f} passes={row['passes_ok']} "
            f"claims={row['claims']} {row['seconds']}s{halluc}"
        )

    total = len(rows)
    accuracy = sum(r["correct"] for r in rows) / total
    total_claims = sum(r["claims"] for r in rows)
    hallucinated = sum(r["hallucinated"] for r in rows)
    rate = hallucinated / total_claims if total_claims else 0.0
    approved = sum(r["observer_approved"] for r in rows)

    print("\n" + "=" * 78)
    print(f"RCA accuracy          : {accuracy:.1%}  ({sum(r['correct'] for r in rows)}/{total})"
          f"   target >= 85%   {'OK' if accuracy >= 0.85 else 'FAIL'}")
    print(f"Hallucination rate    : {rate:.2%}  ({hallucinated}/{total_claims} claims)"
          f"   target < 5%     {'OK' if rate < 0.05 else 'FAIL'}")
    print(f"Observer approved     : {approved}/{total}")
    print(f"Category supported    : {sum(r['category_supported'] for r in rows)}/{total}")
    print(f"Degraded ensembles    : {sum(r['ensemble_degraded'] for r in rows)}/{total}")
    print(f"Low-confidence flagged: {sum(r['low_confidence'] for r in rows)}/{total}")
    print(f"Total tokens          : {sum(r['tokens'] for r in rows):,}")
    print(f"Models used           : {sorted({m for r in rows for m in r['models'].split(',') if m})}")
    print("=" * 78)

    misses = [r for r in rows if not r["correct"]]
    if misses:
        print("\nMisses (expected -> got):")
        for r in misses:
            print(f"  {r['name']}: {r['expected']} -> {r['got']}")

    close = getattr(provider, "aclose", None)
    if close:
        await close()
    return 0 if (accuracy >= 0.85 and rate < 0.05) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
