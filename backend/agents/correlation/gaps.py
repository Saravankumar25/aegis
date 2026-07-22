"""Sanitisation for documented evidence gaps (FR-16, ESD §16).

A "gap" is what Aegis records when an evidence source fails: ``source: reason``, where
``reason`` is the **error string an MCP tool handed back**. That string is attacker-reachable
in exactly the way a log line is — it can carry an upstream response body, a proxy error page,
or a GitHub API message — and until this module existed it took a path that no other untrusted
text in the system takes:

* ``EvidenceStore.add`` redacts and ``<evidence>``-wraps its text. ``note_gap`` did neither, so
  a PAN or an email inside an error message reached prompts, logs and traces in the clear.
* Gap text is rendered into **four** prompts (``correlation.plan``, ``correlation.synthesis``,
  ``rca.hypothesis``, ``observer.critique``) *outside* any ``<evidence>`` tag — and
  ``EVIDENCE_RULES`` only claims authority over text **inside** those tags. Untagged text
  therefore reads as system-authored narration, which is a stronger position than the injected
  log lines the whole delimiting scheme exists to neutralise.

So gap reasons are treated as what they are: untrusted text. Redacted, tag-defanged, and
length-bounded before they are stored.

Sanitising at the origin rather than at render time is deliberate — there are four render
sites and one origin, and the stored value is also what reaches logs and the DB. The durable
home for this is ``EvidenceStore.note_gap`` itself; it lives here because the gap-producing
call sites are all in this package.
"""

from __future__ import annotations

import re

from redaction.pipeline import redact

# Gaps are rendered as one bullet per line. A multi-line upstream error (an HTML error page,
# a stack trace) would otherwise forge additional bullets and appear to be several distinct
# documented gaps rather than one.
_NEWLINES = re.compile(r"[\r\n]+")

# Gap text is not wrapped in <evidence>, so a literal tag inside it could open a data region
# the model then believes it has left, or close one opened elsewhere in the prompt.
_TAGS = re.compile(r"</?\s*(evidence|system|assistant|human|instructions?)\b", re.IGNORECASE)

MAX_GAP_REASON_CHARS = 300


def sanitize_gap_reason(reason: str) -> str:
    """Redact, defang and bound one MCP error string before it becomes a documented gap.

    Length is bounded because an error that echoes a response body would otherwise consume
    the incident token budget (ESD §15) with content that says nothing beyond "this failed".
    """
    collapsed = _NEWLINES.sub(" ", str(reason)).strip() or "unavailable"
    defanged = _TAGS.sub("[defanged-tag]", collapsed)
    cleaned = redact(defanged).text
    if len(cleaned) > MAX_GAP_REASON_CHARS:
        cleaned = cleaned[:MAX_GAP_REASON_CHARS] + "… (truncated)"
    return cleaned
