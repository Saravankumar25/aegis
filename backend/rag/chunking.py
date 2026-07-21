"""Document chunking for the runbook corpus (ESD §20).

Chunking exists because embedding a whole document produces one vector that is the *average*
of everything it discusses. A runbook covering OOM diagnosis, mitigation, and rollback collapses
into a single blurred point that matches every one of those queries weakly and none of them
well. It also makes citations useless: a citation to a 200-line document does not tell a
responder where to look, which defeats the grounding requirement it exists to serve.

The strategy is **structure-aware**: operational runbooks are Markdown with meaningful headings,
so splitting on heading boundaries keeps a procedure intact instead of severing it mid-step.
Oversized sections fall back to paragraph packing, and a pathological single paragraph falls
back to a hard character split — every document chunks, regardless of shape.

Each chunk carries its heading trail as a prefix. Retrieval otherwise strips the context that
makes a fragment interpretable: "set `memory.limit` to 512Mi" means very different things under
"Mitigation" than under "Known false positives".

Pure functions over strings — no I/O, no model, no database — so the strategy is testable in
isolation from everything else in the pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable unit of a document."""

    index: int
    content: str
    heading_path: str

    @property
    def embedding_text(self) -> str:
        """What actually gets embedded: heading trail plus body.

        The heading trail is prepended rather than stored separately because the embedding
        must encode the context, not merely travel beside it.
        """
        return f"{self.heading_path}\n\n{self.content}" if self.heading_path else self.content


@dataclass(frozen=True, slots=True)
class ChunkConfig:
    """Chunking strategy parameters.

    ``max_chars``/``overlap_chars`` are character-based rather than token-based on purpose: it
    keeps this module free of a tokenizer dependency, and for English prose the ratio is stable
    enough (~4 chars/token) that a character budget maps predictably onto the model's window.
    """

    max_chars: int = 1200
    overlap_chars: int = 150
    min_chars: int = 80

    def __post_init__(self) -> None:
        if self.overlap_chars >= self.max_chars:
            # Would never advance: each chunk would start at or before its predecessor.
            raise ValueError("overlap_chars must be smaller than max_chars")


def _split_oversized(text: str, config: ChunkConfig) -> list[str]:
    """Pack paragraphs up to the budget, with overlap; hard-split anything still too large."""
    paragraphs = [p.strip() for p in _PARAGRAPH_BREAK.split(text) if p.strip()]
    packed: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > config.max_chars:
            if current:
                packed.append(current)
                current = ""
            # A single paragraph beyond the budget (a long table or log dump). Slice it with
            # overlap so a fact spanning a boundary still appears whole in one chunk.
            step = config.max_chars - config.overlap_chars
            for start in range(0, len(paragraph), step):
                packed.append(paragraph[start : start + config.max_chars])
            continue

        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= config.max_chars:
            current = candidate
        else:
            packed.append(current)
            # Carry the tail of the previous chunk forward as overlap.
            tail = current[-config.overlap_chars :] if config.overlap_chars else ""
            current = f"{tail}\n\n{paragraph}".strip() if tail else paragraph

    if current:
        packed.append(current)
    return packed


def chunk_markdown(text: str, config: ChunkConfig | None = None) -> list[Chunk]:
    """Split a Markdown document into retrievable chunks with heading context."""
    config = config or ChunkConfig()

    # Walk the document accumulating sections under their heading trail.
    sections: list[tuple[str, list[str]]] = []
    heading_stack: list[tuple[int, str]] = []
    body: list[str] = []

    def _current_path() -> str:
        return " › ".join(title for _, title in heading_stack)

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match is None:
            body.append(line)
            continue
        if body and any(line.strip() for line in body):
            sections.append((_current_path(), body))
        body = []
        level = len(match.group(1))
        # Pop headings at or below this level so the trail reflects real nesting.
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, match.group(2).strip()))

    if body and any(line.strip() for line in body):
        sections.append((_current_path(), body))

    chunks: list[Chunk] = []
    for heading_path, lines in sections:
        section_text = "\n".join(lines).strip()
        if not section_text:
            continue
        if len(section_text) <= config.max_chars:
            # A whole section is kept regardless of length. `min_chars` must NOT apply here:
            # a terse but complete instruction ("Raise the memory limit and redeploy") is
            # exactly the passage a responder wants, and dropping it for being short would
            # silently remove the most actionable content in the corpus.
            chunks.append(Chunk(index=len(chunks), content=section_text, heading_path=heading_path))
            continue

        for piece in _split_oversized(section_text, config):
            piece = piece.strip()
            # Here `min_chars` is appropriate: these are fragments produced by splitting, and
            # a sliver left at a boundary carries no meaning while adding index noise.
            if len(piece) < config.min_chars:
                continue
            chunks.append(Chunk(index=len(chunks), content=piece, heading_path=heading_path))

    # A document with no headings and no blank lines still has to be retrievable.
    if not chunks and text.strip():
        for piece in _split_oversized(text.strip(), config):
            chunks.append(Chunk(index=len(chunks), content=piece, heading_path=""))

    return chunks
