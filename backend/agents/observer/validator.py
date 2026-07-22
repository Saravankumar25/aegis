"""Observer Agent core: citation validation + injection screening (FR-8.1, ESD §16).

Deterministic and strict: a claim citing an evidence id that does not exist is rejected
outright (FR-3.2); evidence whose text matches instruction-like patterns is flagged and the
verdict records it, so the RCA layer can exclude it on revision. No LLM involved — the
watchdog must not share the failure modes of the thing it watches.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from agents.evidence import EvidenceStore
from db.enums import EvidenceType

# Instruction-like patterns that have no business inside infrastructure evidence.
INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"ignore (all |any )?(previous|prior|above) instructions",
        r"disregard (the|your|all)\b",
        r"you are now\b",
        r"new instructions?:",
        r"system prompt",
        r"</?(system|assistant|human|instructions?)>",
        r"\bdo not (tell|report|mention)\b",
        r"\breveal\b.{0,40}\b(secret|key|token|password)",
        r"(execute|run) (the following|this) (command|code)",
    )
]


class ClaimVerdict(BaseModel):
    claim: str
    evidence_id: str | None
    valid: bool
    reason: str


class ObserverVerdict(BaseModel):
    """The Observer's decision about one RCA result."""

    approved: bool
    claim_verdicts: list[ClaimVerdict] = Field(default_factory=list)
    rejected_count: int = 0
    flagged_evidence: list[dict] = Field(default_factory=list)  # {evidence_id, pattern}
    # Injection patterns found in *documented gaps* rather than in evidence. Tracked
    # separately because nothing can cite a gap, so this can never feed `cites_poisoned`
    # — it is a signal for the operator, not an input to the approval decision.
    flagged_gaps: list[dict] = Field(default_factory=list)  # {gap, pattern}
    category_supported: bool = True
    category_reason: str = ""
    notes: str = ""


# What each root-cause category REQUIRES in the cited evidence before it may be
# asserted. A resolving citation is necessary but not sufficient: a model can cite
# a real pod list while claiming a deploy caused the outage, which is a grounded-
# looking claim about evidence that says nothing of the kind. Observed in a live
# run — the model blamed "a recent deployment" while the GitHub source was down and
# no deploy evidence existed at all.
#
# `markers` are REGEXES, not substrings, matched case-insensitively against the
# redacted snippets of the cited evidence; `requires_type` additionally demands a
# specific evidence kind.
#
# Regexes rather than substrings because the naive version matched healthy
# readings: "restarts=0" contains "restart", so a perfectly healthy pod satisfied
# the resource-exhaustion check. A support marker must match the *presence of a
# fault*, not merely the presence of the word for it.
CATEGORY_SUPPORT: dict[str, dict] = {
    "deploy_regression": {
        "requires_type": EvidenceType.diff,
        "markers": [
            r"\bcommit\s+[0-9a-f]{6,}",
            r"\bdeploy(ed|ment)?\b",
            r"\bmerged\b",
            r"\brollback\b",
            r"\breleased?\b",
        ],
        "why": "a deploy/change record must be cited before blaming a code change",
    },
    "resource_exhaustion": {
        "requires_type": None,
        "markers": [
            # Compute exhaustion.
            r"oomkill",
            r"out of memory",
            r"memory limit",
            r"crashloop",
            r"back-off",
            r"restarts?\s*[=:]?\s*[1-9]",  # non-zero restarts only
            r"\brestarting\b",
            r"\bevicted\b",
            r"cpu throttl",
            # Handle exhaustion. Added after a real rejection: the evidence read
            # "pool exhausted, 0 idle connections" and RCA correctly called it resource
            # exhaustion, but the marker list only recognised *memory* exhaustion, so a
            # correct hypothesis was rejected for citing the wrong kind of resource. These
            # stay high-precision — each names a specific exhausted resource, so the rule
            # still refuses a hypothesis backed by nothing but a generic error line.
            r"pool\s+(is\s+)?exhaust",
            r"connection pool",
            r"\b0 idle connections?\b",
            r"no (idle|available) connections?",
            r"too many open files",
            r"file descriptors?\s+(exhaust|limit)",
            r"thread pool\s+(exhaust|full)",
            r"queue (is )?full",
            r"\bbackpressure\b",
        ],
        "why": (
            "a signal naming an exhausted resource (memory, CPU, connections, handles) "
            "must be cited before blaming resources"
        ),
    },
    "latency_degradation": {
        "requires_type": None,
        "markers": [
            r"\blatency\b",
            r"\bslow\b",
            r"\btimed?\s*out\b|\btimeout\b",
            r"\bp9[59]\b",
            r"duration",
            r"saturat",
        ],
        "why": "a latency/timeout signal must be cited before blaming slowness",
    },
    "error_spike": {
        "requires_type": None,
        # Requires a NON-ZERO 5xx signal or an explicit error/exception line: a
        # cited "rate(status=500) = 0" is evidence of health, not of an error spike.
        "markers": [
            r"status\s*=\s*\"?5\d\d\"?\)?\s*=\s*(?!0(?:\.0+)?\s*(?:/s)?\b)",
            r"\b5xx\b",
            r"\berror\b(?!\s*rate\s*=\s*0)",
            r"\bexception\b",
            r"\bfatal\b",
            r"\bhttp\s*5\d\d\b",
        ],
        "why": "an error-rate signal must be cited before blaming an error spike",
    },
    "unknown": {"requires_type": None, "markers": [], "why": ""},
}


def check_category_support(
    root_cause_category: str, claims: list[dict], store: EvidenceStore
) -> tuple[bool, str]:
    """Is the asserted category actually backed by the evidence the claims cite?

    Returns ``(supported, reason)``. This is the guard against the subtle failure
    mode where every citation resolves but none of the cited evidence says anything
    about the cause being asserted.
    """
    rule = CATEGORY_SUPPORT.get(root_cause_category)
    if rule is None:
        return False, f"'{root_cause_category}' is not a recognised root-cause category"
    if root_cause_category == "unknown":
        return True, "no cause asserted"

    cited = [store.get(str(c.get("evidence_id"))) for c in claims if c.get("evidence_id")]
    cited_items = [item for item in cited if item is not None]
    if not cited_items:
        return False, "no resolvable evidence cited"

    required_type = rule["requires_type"]
    if required_type is not None and not any(i.type == required_type for i in cited_items):
        available = {i.type for i in store.items}
        detail = (
            f"no {required_type} evidence was gathered at all"
            if required_type not in available
            else f"{required_type} evidence exists but none was cited"
        )
        return False, f"{rule['why']} — {detail}"

    haystack = " ".join(i.summary for i in cited_items)
    if rule["markers"] and not any(re.search(m, haystack, re.IGNORECASE) for m in rule["markers"]):
        return False, f"{rule['why']} — cited evidence contains no supporting signal"
    return True, "cited evidence supports the asserted category"


def screen_evidence(store: EvidenceStore) -> list[dict]:
    """Flag evidence whose text looks like instructions rather than observations."""
    flagged: list[dict] = []
    for item in store.items:
        for pattern in INJECTION_PATTERNS:
            if pattern.search(item.summary):
                flagged.append({"evidence_id": item.id, "pattern": pattern.pattern})
                break
    return flagged


def screen_gaps(store: EvidenceStore) -> list[dict]:
    """Flag documented gaps whose text looks like instructions rather than an error.

    A gap's text is the error string an MCP tool returned, so it is attacker-reachable the
    same way a log line is — and it lands in four prompts *outside* any ``<evidence>`` tag,
    where ``EVIDENCE_RULES`` does not reach. ``screen_evidence`` only ever looked at
    ``store.items``, so this channel was invisible to the Observer entirely.

    This **records and does not block**, matching the project's ingress-fails-open rule: a
    gap is produced by a source *failing*, so letting flagged gap text veto an investigation
    would let anyone who can make an MCP call fail with a crafted error message shut incident
    response down. Blocking here would build the denial-of-service the convention exists to
    prevent.
    """
    flagged: list[dict] = []
    for gap in store.gaps:
        for pattern in INJECTION_PATTERNS:
            if pattern.search(gap):
                flagged.append({"gap": gap[:200], "pattern": pattern.pattern})
                break
    return flagged


def validate_claims(claims: list[dict], store: EvidenceStore) -> list[ClaimVerdict]:
    """FR-3.2: every claim must cite a real piece of gathered evidence."""
    verdicts: list[ClaimVerdict] = []
    for claim in claims:
        text = str(claim.get("claim", ""))
        evidence_id = claim.get("evidence_id")
        if not evidence_id:
            verdicts.append(
                ClaimVerdict(
                    claim=text, evidence_id=None, valid=False, reason="no citation attached"
                )
            )
            continue
        item = store.get(str(evidence_id))
        if item is None:
            verdicts.append(
                ClaimVerdict(
                    claim=text,
                    evidence_id=str(evidence_id),
                    valid=False,
                    reason="cited evidence id does not exist",
                )
            )
            continue
        verdicts.append(
            ClaimVerdict(claim=text, evidence_id=item.id, valid=True, reason="citation resolves")
        )
    return verdicts


def review(
    claims: list[dict], store: EvidenceStore, root_cause_category: str | None = None
) -> ObserverVerdict:
    """Full Observer pass: citations + category support + injection screen (FR-8.1).

    Three independent things must hold before a hypothesis reaches a human:
    every claim cites evidence that exists, none of the cited evidence looks like
    injected instructions, and — when a category is asserted — the cited evidence
    actually supports that category rather than merely existing.
    """
    claim_verdicts = validate_claims(claims, store)
    flagged = screen_evidence(store)
    flagged_gaps = screen_gaps(store)
    rejected = [v for v in claim_verdicts if not v.valid]
    poisoned_ids = {f["evidence_id"] for f in flagged}
    cites_poisoned = [v for v in claim_verdicts if v.valid and v.evidence_id in poisoned_ids]

    category_supported, category_reason = True, ""
    if root_cause_category is not None:
        category_supported, category_reason = check_category_support(
            root_cause_category, claims, store
        )

    approved = not rejected and not cites_poisoned and bool(claim_verdicts) and category_supported
    notes = []
    if rejected:
        notes.append(f"{len(rejected)} claim(s) rejected for missing/invalid citations")
    if cites_poisoned:
        notes.append(f"{len(cites_poisoned)} claim(s) cite injection-flagged evidence")
    if not claim_verdicts:
        notes.append("no claims presented")
    if not category_supported:
        notes.append(f"unsupported root cause: {category_reason}")
    if flagged_gaps:
        # Surfaced, never subtracted from `approved` — see `screen_gaps`.
        notes.append(f"{len(flagged_gaps)} documented gap(s) contain instruction-like text")
    return ObserverVerdict(
        approved=approved,
        claim_verdicts=claim_verdicts,
        rejected_count=len(rejected) + len(cites_poisoned),
        flagged_evidence=flagged,
        flagged_gaps=flagged_gaps,
        category_supported=category_supported,
        category_reason=category_reason,
        notes="; ".join(notes) or "all claims validated",
    )
