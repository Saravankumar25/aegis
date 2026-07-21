"""Unit tests: the stub provider must be grounded — it can only cite real evidence ids."""

from __future__ import annotations

import json

from providers.stub import StubProvider
from redaction.pipeline import wrap_evidence


def _prompt(*blocks: tuple[str, str]) -> str:
    body = "\n".join(wrap_evidence(i, "test", t) for i, t in blocks)
    return f"instructions\n{body}\nrespond with JSON"


async def test_rca_cites_only_present_evidence_ids():
    prompt = _prompt(("E1", "pod OOMKilled restarts=7"), ("E2", "all quiet"))
    result = await StubProvider().complete(prompt, agent="rca")
    parsed = json.loads(result.text)
    cited = {c["evidence_id"] for c in parsed["claims"]}
    assert cited <= {"E1", "E2"}
    assert parsed["root_cause_category"] == "resource_exhaustion"


async def test_no_signals_yields_unknown_with_no_claims():
    prompt = _prompt(("E1", "everything is completely fine"))
    parsed = json.loads((await StubProvider().complete(prompt, agent="rca")).text)
    assert parsed["root_cause_category"] == "unknown"
    assert parsed["claims"] == []


async def test_mixed_signals_disagree_across_ensemble_passes():
    # Deploy + OOM signals present: pass 1's resource bias should flip the category.
    prompt = _prompt(
        ("E1", "commit 9f1c2e3 deploy: raise cache ttl"),
        ("E2", "pod OOMKilled CrashLoopBackOff restart"),
    )
    provider = StubProvider()
    p0 = json.loads((await provider.complete(prompt, agent="rca", ensemble_pass=0)).text)
    p1 = json.loads((await provider.complete(prompt, agent="rca", ensemble_pass=1)).text)
    assert p0["root_cause_category"] != p1["root_cause_category"]


async def test_accounting_fields_populated():
    result = await StubProvider().complete(_prompt(("E1", "error rate")), agent="rca")
    assert result.tokens_used > 0
    assert result.cost_usd == 0.0
    assert result.model == "stub-deterministic"
