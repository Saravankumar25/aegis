"""Input and output guardrails for every LLM interaction (ESD §16, FR-16).

Two gates around each model call:

``guard_input``  — what may reach the model.
``guard_output`` — what the model is allowed to return to the rest of the system.

Both classify severity rather than returning a bare boolean, because the correct response
genuinely differs. An injection attempt inside *evidence* must not abort the incident — that
is precisely the outcome an attacker wants, and the existing pipeline already handles it by
delimiting the evidence and letting the Observer exclude it. But a secret leaking *out* of a
model into a Slack message is unrecoverable once sent, so it is blocked outright.

The design rule throughout: **fail closed on egress, fail loud-but-open on ingress.**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from core.logging import get_logger
from redaction.pipeline import redact

_log = get_logger(component="guardrails")


class Severity(StrEnum):
    """How a violation should be handled."""

    # Recorded and passed through — the pipeline already contains this risk by design.
    observe = "observe"
    # Content is modified (redacted / stripped) before it continues.
    sanitize = "sanitize"
    # The call does not proceed, or the output does not leave.
    block = "block"


class GuardrailViolation(RuntimeError):
    """A blocking guardrail rejected the content."""

    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    severity: Severity
    detail: str


@dataclass(slots=True)
class InputGuardResult:
    prompt: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.severity is Severity.block for f in self.findings)


@dataclass(slots=True)
class OutputGuardResult:
    text: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.severity is Severity.block for f in self.findings)


# --- Jailbreak / instruction-override patterns -------------------------------------------
#
# These target attempts to change the *model's role or rules*, which is different from the
# injection screen in the Observer (that one screens evidence text). Here the concern is any
# content — evidence, a user query, a runbook — carrying role-subversion language.
_JAILBREAK = [
    (
        r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+instructions",
        "instruction_override",
    ),
    (r"disregard\s+(the|your|all|previous)\b", "instruction_override"),
    (r"you\s+are\s+now\s+(a|an|the)\b", "role_reassignment"),
    (r"\bnew\s+instructions?\s*:", "instruction_override"),
    (r"(reveal|print|show|repeat)\b.{0,40}\b(system\s+prompt|instructions)", "prompt_extraction"),
    (r"\b(developer|debug|god)\s+mode\b", "mode_escalation"),
    (r"\bpretend\s+(you|to\s+be)\b", "role_reassignment"),
    (r"\bwithout\s+any\s+(restrictions|filters|rules)\b", "restriction_bypass"),
    (r"</?(system|assistant|human|instructions?)\s*>", "delimiter_forgery"),
    (r"\b(execute|run)\s+(the\s+following|this)\s+(command|code|script)", "code_execution"),
]
_JAILBREAK_PATTERNS = [(re.compile(p, re.IGNORECASE), name) for p, name in _JAILBREAK]

# --- Secret egress patterns ---------------------------------------------------------------
#
# Checked on OUTPUT only. If a model ever emits something shaped like a credential, that
# content must not continue to Slack, a dashboard, or a database — regardless of how it got
# there. This is the one place guardrails fail closed.
_SECRET_EGRESS = [
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private_key"),
    (r"\bsk-[A-Za-z0-9_-]{16,}", "api_key"),
    (r"\bAIza[0-9A-Za-z_-]{35}\b", "google_api_key"),
    (r"\bghp_[A-Za-z0-9]{36}\b", "github_token"),
    (r"\bxox[baprs]-[A-Za-z0-9-]{10,}", "slack_token"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "jwt"),
]
_SECRET_PATTERNS = [(re.compile(p), name) for p, name in _SECRET_EGRESS]

# Phrases that indicate the model is answering from general knowledge rather than from the
# evidence it was given. Not proof of a hallucination, but a reliable smell: grounded output
# cites, ungrounded output hedges.
_UNGROUNDED_MARKERS = [
    (r"\btypically\b|\busually\b|\bgenerally\b", "generic_knowledge"),
    (r"\bit'?s? (likely|probable) that\b", "speculation"),
    (r"\bI (assume|believe|think|guess)\b", "speculation"),
    (r"\bcommonly caused by\b", "generic_knowledge"),
]
_UNGROUNDED_PATTERNS = [(re.compile(p, re.IGNORECASE), name) for p, name in _UNGROUNDED_MARKERS]

MAX_PROMPT_CHARS = 120_000


def guard_input(prompt: str, *, agent: str, trusted: bool = False) -> InputGuardResult:
    """Screen content on its way into a model.

    ``trusted=True`` marks prompt text Aegis authored itself (templates, schemas). Untrusted
    content — anything derived from logs, commits, alerts, or user input — is screened.

    Jailbreak findings are **observe**, not **block**, and that is deliberate. Evidence
    arrives already redacted and delimited, and the Observer excludes flagged evidence on
    revision. Aborting the investigation instead would hand any attacker who can write to a
    log a reliable denial-of-service against incident response. The finding is recorded so
    the attempt is visible.
    """
    findings: list[Finding] = []

    if len(prompt) > MAX_PROMPT_CHARS:
        # Oversized prompts are truncated, never silently sent: an over-budget prompt is
        # rejected by the API or silently trimmed at the far end, losing the *end* of the
        # instructions — which is where the output contract lives.
        findings.append(
            Finding(
                "prompt_too_large",
                Severity.sanitize,
                f"{len(prompt)} chars exceeds {MAX_PROMPT_CHARS}; truncated",
            )
        )
        prompt = prompt[:MAX_PROMPT_CHARS] + "\n[...truncated by guardrails...]"

    if not trusted:
        for pattern, name in _JAILBREAK_PATTERNS:
            if pattern.search(prompt):
                findings.append(
                    Finding(name, Severity.observe, f"jailbreak pattern matched: {name}")
                )
                break

    if findings:
        _log.warning(
            "guardrail_input",
            agent=agent,
            findings=[f.rule for f in findings],
        )
    return InputGuardResult(prompt=prompt, findings=findings)


def guard_output(
    text: str,
    *,
    agent: str,
    require_grounding: bool = False,
    forbid_terms: list[str] | None = None,
) -> OutputGuardResult:
    """Screen a model's output before the rest of the system consumes it.

    Secret egress **blocks**. Everything a model emits can reach Slack, the dashboard, or the
    database, and a leaked credential cannot be recalled once sent — so this is the one gate
    that fails closed rather than degrading.
    """
    findings: list[Finding] = []

    for pattern, name in _SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(
                Finding(name, Severity.block, f"output contains something shaped like a {name}")
            )

    if findings:
        _log.error("guardrail_output_blocked", agent=agent, rules=[f.rule for f in findings])
        raise GuardrailViolation(
            findings[0].rule,
            "model output was withheld because it contained credential-shaped content",
        )

    # PII that survived to the output is redacted rather than blocked: the analysis is still
    # useful, and the redaction pipeline is the same one applied to evidence on ingress.
    redacted = redact(text)
    if redacted.text != text:
        findings.append(Finding("pii_egress", Severity.sanitize, "output redacted before use"))
        text = redacted.text

    if forbid_terms:
        # Used by the Communication agent: an internal identifier must never reach a
        # stakeholder update, and asking the model nicely is not an enforcement mechanism.
        hits = [t for t in forbid_terms if t and t.lower() in text.lower()]
        if hits:
            findings.append(
                Finding(
                    "forbidden_term",
                    Severity.block,
                    f"output leaked internal terms: {sorted(set(hits))}",
                )
            )
            _log.error("guardrail_output_blocked", agent=agent, rule="forbidden_term")
            raise GuardrailViolation(
                "forbidden_term", f"output contained internal identifiers: {sorted(set(hits))}"
            )

    if require_grounding:
        for pattern, name in _UNGROUNDED_PATTERNS:
            if pattern.search(text):
                findings.append(
                    Finding(name, Severity.observe, f"ungrounded-language marker: {name}")
                )
                break

    if findings:
        _log.info("guardrail_output", agent=agent, findings=[f.rule for f in findings])
    return OutputGuardResult(text=text, findings=findings)
