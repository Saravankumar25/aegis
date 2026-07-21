"""Deterministic stub provider — the default (ESD §20: zero-key, zero-cost local runs).

Grounded by construction: it only ever cites evidence ids that actually appear in the
prompt's ``<evidence>`` blocks, and derives its root-cause category from keyword signals in
that evidence. Ensemble passes vary the signal weighting, so a clean single-cause incident
produces high agreement and a mixed-signal incident produces visible disagreement —
exercising the FR-3.1 agreement scoring realistically without an API key.
"""

from __future__ import annotations

import json
import re
import time

from providers.base import LLMProvider, LLMResult

_EVIDENCE_BLOCK = re.compile(
    r'<evidence id="(?P<id>[^"]+)" source="(?P<source>[^"]+)">\n(?P<body>.*?)\n</evidence>',
    re.DOTALL,
)

# (category, [keywords], base weight). Order = default priority when weights tie.
# Keywords are deliberately specific: bare "restart" would match the healthy
# "restarts=0" in every pod summary, and "commit"/"deploy" would match the
# "no code changes found" message — both would fabricate signal from absence.
_SIGNALS: list[tuple[str, list[str], float]] = [
    ("deploy_regression", ["commit ", "deployed", "merged", "feat:", "fix:", "rollback"], 1.0),
    (
        "resource_exhaustion",
        ["oomkilled", "crashloopbackoff", "out of memory", "memory limit", "back-off"],
        0.9,
    ),
    ("error_spike", ['status="500"', "status=500", "5xx", "error rate", "http 500"], 0.7),
    ("latency_degradation", ["latency", "p99", "slow", "timeout"], 0.6),
]

# Per-ensemble-pass multiplier: pass 1 leans harder on resource signals, so mixed-signal
# incidents disagree across passes while single-cause ones stay unanimous.
_PASS_BIAS: dict[int, dict[str, float]] = {
    0: {},
    1: {"resource_exhaustion": 1.4, "deploy_regression": 0.8},
    2: {},
}


class StubProvider(LLMProvider):
    """Deterministic, evidence-grounded fake LLM."""

    name = "stub"

    async def complete(
        self, prompt: str, *, agent: str, ensemble_pass: int = 0, max_tokens: int = 1024
    ) -> LLMResult:
        start = time.perf_counter()
        if agent == "rca":
            text = self._rca(prompt, ensemble_pass)
        else:
            text = json.dumps({"summary": prompt[:200]})
        latency_ms = int((time.perf_counter() - start) * 1000)
        # Deterministic pseudo-accounting: ~4 chars/token, zero cost.
        tokens = max(1, (len(prompt) + len(text)) // 4)
        return LLMResult(
            text=text,
            model="stub-deterministic",
            tokens_used=tokens,
            cost_usd=0.0,
            latency_ms=latency_ms,
        )

    def _rca(self, prompt: str, ensemble_pass: int) -> str:
        evidence = [
            (m.group("id"), m.group("source"), m.group("body").lower())
            for m in _EVIDENCE_BLOCK.finditer(prompt)
        ]
        bias = _PASS_BIAS.get(ensemble_pass % 3, {})
        scores: dict[str, float] = {}
        supporting: dict[str, list[str]] = {}
        for category, keywords, weight in _SIGNALS:
            for ev_id, _source, body in evidence:
                if any(k in body for k in keywords):
                    scores[category] = scores.get(category, 0.0) + weight * bias.get(category, 1.0)
                    supporting.setdefault(category, []).append(ev_id)

        if not scores:
            return json.dumps(
                {
                    "root_cause_category": "unknown",
                    "hypothesis": "Insufficient evidence to form a hypothesis.",
                    "confidence": 0.2,
                    "claims": [],
                }
            )

        best = max(scores, key=lambda c: (scores[c], -[s[0] for s in _SIGNALS].index(c)))
        cited = sorted(set(supporting[best]))[:3]
        confidence = min(0.95, 0.5 + 0.15 * len(cited) + 0.05 * scores[best])
        hypothesis = {
            "deploy_regression": "A recent code change regressed the service",
            "resource_exhaustion": "The service is resource-starved (OOM/crash-looping)",
            "error_spike": "An upstream/internal fault is producing an elevated error rate",
            "latency_degradation": "A dependency or saturation issue is degrading latency",
        }[best]
        return json.dumps(
            {
                "root_cause_category": best,
                "hypothesis": hypothesis,
                "confidence": round(confidence, 2),
                "claims": [
                    {
                        "claim": f"Evidence {ev_id} is consistent with {best}",
                        "evidence_id": ev_id,
                    }
                    for ev_id in cited
                ],
            }
        )
