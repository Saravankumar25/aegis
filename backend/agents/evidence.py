"""Evidence model shared by the investigation agents (ESD §9 step 3, FR-2.1).

Every piece of evidence enters through ``EvidenceStore.add``: the raw text is redacted and
delimiter-wrapped exactly once, right here — downstream code only ever sees the redacted
summary and the wrapped block, so there is no path from an MCP payload into a prompt, log,
or DB row that bypasses the pipeline (ESD §24 redaction middleware).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from db.enums import EvidenceType
from redaction.pipeline import redact, wrap_evidence

MAX_SNIPPET_CHARS = 1_500  # keep prompts inside the incident token budget (ESD §15)


class EvidenceItem(BaseModel):
    """One redacted, delimited piece of evidence with a stable citation id."""

    id: str  # E1, E2, ... — the id RCA claims cite (FR-3.2)
    type: EvidenceType
    source: str  # MCP verb_noun tool that produced it
    ref: str  # machine ref, e.g. "k8s/pod/checkout-..-log" or "github/commit/9f1c2e3"
    summary: str  # redacted snippet (what gets persisted in evidence_citations)
    wrapped: str  # <evidence>-delimited block (what goes into prompts)
    untrusted: bool = True


class EvidenceStore(BaseModel):
    """Ordered evidence plus explicitly documented gaps (PRD 11A: note unavailable sources)."""

    items: list[EvidenceItem] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    def add(self, *, type_: EvidenceType, source: str, ref: str, text: str) -> EvidenceItem:
        evidence_id = f"E{len(self.items) + 1}"
        snippet = text[:MAX_SNIPPET_CHARS]
        item = EvidenceItem(
            id=evidence_id,
            type=type_,
            source=source,
            ref=ref,
            summary=redact(snippet).text,
            wrapped=wrap_evidence(evidence_id, source, snippet),
        )
        self.items.append(item)
        return item

    def note_gap(self, source: str, reason: str) -> str:
        """Record an unavailable evidence source, sanitising the reason first.

        Returns the sanitised reason so a caller that needs the text — to return it, log it,
        or show it — gets the safe version without sanitising again. Handing back the raw
        argument would leave every caller one forgotten call away from reintroducing the
        bypass this method exists to close.

        Sanitisation lives *here*, not at the call sites, and that placement is the point.
        A gap reason is the error string an MCP tool returned — attacker-reachable exactly
        as a log line is — and it renders into four prompts (`correlation.plan`,
        `correlation.synthesis`, `rca.hypothesis`, `observer.critique`). It originally
        bypassed redaction and `<evidence>` delimiting entirely, so a card number in an
        upstream 500 reached the RCA prompt in the clear, and the text landed *outside* any
        evidence tag — where the rules governing tagged content do not reach, making it read
        as system-authored narration rather than as untrusted data.

        Sanitising at the seven known call sites fixed the instances but not the class: the
        next gap producer would reintroduce it. Doing it in the one method every producer
        must call makes bypassing it require deleting this line.
        """
        from agents.correlation.gaps import sanitize_gap_reason

        safe = sanitize_gap_reason(reason)
        self.gaps.append(f"{source}: {safe}")
        return safe

    def get(self, evidence_id: str) -> EvidenceItem | None:
        return next((i for i in self.items if i.id == evidence_id), None)

    def prompt_block(self) -> str:
        """All wrapped evidence, ready to drop into a prompt after EVIDENCE_RULES."""
        return "\n".join(i.wrapped for i in self.items)
