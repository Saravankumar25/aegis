"""Test doubles: a replay gateway and a recorded-response LLM.

NOT product code. The application has exactly one evidence gateway (``McpGateway``,
real MCP over stdio) and exactly one LLM provider (``OpenRouterProvider``, real
models). These doubles exist only so the test suite is deterministic and CI spends
no tokens — they are unreachable from any runtime path because nothing under
``backend/`` outside ``tests/`` imports this module.

``RecordedLLM`` can replay verbatim model responses passed to it; with no responses
supplied it emits a minimal schema-valid object citing only evidence ids that
actually appear in the prompt. It never invents a citation, so no test can pass by
asserting on a hallucinated one. It is a structural fixture for the plumbing —
model *quality* is measured only by ``eval/run_real_eval.py`` against live models.
"""

from __future__ import annotations

import json
import re
from typing import Any

from providers.base import LLMProvider, LLMResult

ToolResultDict = dict[str, Any]


def unavailable(source: str, tool: str, reason: str) -> ToolResultDict:
    return {
        "ok": False,
        "source": source,
        "tool": tool,
        "error_kind": "unavailable",
        "error": reason,
        "contains_untrusted_text": False,
        "data": None,
        "attempts": 0,
    }


class ReplayGateway:
    """Replays recorded MCP tool results keyed by ``(server, tool)``."""

    def __init__(self, recorded: dict[tuple[str, str], ToolResultDict]) -> None:
        self._recorded = recorded
        self.calls: list[tuple[str, str, dict]] = []

    async def start(self) -> None:  # interface parity with McpGateway
        return None

    async def stop(self) -> None:
        return None

    async def call(self, server: str, tool: str, arguments: dict | None = None) -> ToolResultDict:
        self.calls.append((server, tool, arguments or {}))
        result = self._recorded.get((server, tool))
        if result is None:
            # An un-recorded tool behaves exactly like an unreachable MCP server,
            # which is the behaviour the gap-handling tests depend on.
            return unavailable(server, tool, "no recorded response")
        return result


_EVIDENCE_BLOCK = re.compile(r'<evidence id="([^"]+)"', re.MULTILINE)


class RecordedLLM(LLMProvider):
    """Replays a real captured model response, or derives one from the prompt.

    ``responses`` maps a substring of the prompt to the exact text a real model
    returned for it. When nothing matches, the double emits a minimal valid RCA
    object citing evidence ids that genuinely appear in the prompt — never invented
    ones, so tests can never accidentally assert on a hallucinated citation.
    """

    name = "recorded"

    def __init__(
        self,
        responses: dict[str, str] | None = None,
        *,
        category: str = "resource_exhaustion",
        confidence: float = 0.85,
    ) -> None:
        self._responses = responses or {}
        self._category = category
        self._confidence = confidence
        self.calls: list[tuple[str, int]] = []

    async def complete(
        self, prompt: str, *, agent: str, ensemble_pass: int = 0, max_tokens: int = 1024
    ) -> LLMResult:
        self.calls.append((agent, ensemble_pass))
        for needle, text in self._responses.items():
            if needle in prompt:
                return self._result(text)
        if agent != "rca":
            return self._result(json.dumps({"summary": prompt[:200]}))
        evidence_ids = _EVIDENCE_BLOCK.findall(prompt)[:3]
        return self._result(
            json.dumps(
                {
                    "root_cause_category": self._category if evidence_ids else "unknown",
                    "hypothesis": f"Recorded test response for {self._category}.",
                    "confidence": self._confidence,
                    "claims": [
                        {"claim": f"Evidence {eid} supports this", "evidence_id": eid}
                        for eid in evidence_ids
                    ],
                }
            )
        )

    @staticmethod
    def _result(text: str) -> LLMResult:
        return LLMResult(
            text=text,
            model="recorded-test-double",
            tokens_used=len(text) // 4,
            cost_usd=0.0,
            latency_ms=0,
        )


class FailingLLM(LLMProvider):
    """Always raises — proves the pipeline fails loudly instead of fabricating."""

    name = "failing"

    async def complete(
        self, prompt: str, *, agent: str, ensemble_pass: int = 0, max_tokens: int = 1024
    ) -> LLMResult:
        from providers.openrouter import ProviderExhausted

        raise ProviderExhausted("no capacity (test double)")
