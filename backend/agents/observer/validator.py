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
    notes: str = ""


def screen_evidence(store: EvidenceStore) -> list[dict]:
    """Flag evidence whose text looks like instructions rather than observations."""
    flagged: list[dict] = []
    for item in store.items:
        for pattern in INJECTION_PATTERNS:
            if pattern.search(item.summary):
                flagged.append({"evidence_id": item.id, "pattern": pattern.pattern})
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


def review(claims: list[dict], store: EvidenceStore) -> ObserverVerdict:
    """Full Observer pass: citations + injection screen → approve or send back (FR-8.1)."""
    claim_verdicts = validate_claims(claims, store)
    flagged = screen_evidence(store)
    rejected = [v for v in claim_verdicts if not v.valid]
    poisoned_ids = {f["evidence_id"] for f in flagged}
    cites_poisoned = [v for v in claim_verdicts if v.valid and v.evidence_id in poisoned_ids]
    approved = not rejected and not cites_poisoned and bool(claim_verdicts)
    notes = []
    if rejected:
        notes.append(f"{len(rejected)} claim(s) rejected for missing/invalid citations")
    if cites_poisoned:
        notes.append(f"{len(cites_poisoned)} claim(s) cite injection-flagged evidence")
    if not claim_verdicts:
        notes.append("no claims presented")
    return ObserverVerdict(
        approved=approved,
        claim_verdicts=claim_verdicts,
        rejected_count=len(rejected) + len(cites_poisoned),
        flagged_evidence=flagged,
        notes="; ".join(notes) or "all claims validated",
    )
