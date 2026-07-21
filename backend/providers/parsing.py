"""Provider-neutral parsing helpers for LLM output.

Lives outside any concrete provider so agent code can depend on it without
importing (and coupling to) a specific vendor — agents talk to the ``LLMProvider``
interface, and this is the matching utility for the text that comes back.

Real models emit two formatting failures constantly, even with JSON mode on:
markdown code fences, and a valid JSON object wrapped in explanatory prose.
Both are recoverable without a second round trip, so recover rather than burn an
ensemble pass on a formatting quirk.
"""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def strip_code_fences(text: str) -> str:
    """Return ``text`` with any surrounding markdown code fence removed."""
    return _FENCE.sub("", text).strip()


def parse_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort strict-JSON object extraction. None when it genuinely isn't one."""
    cleaned = strip_code_fences(text)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None
