"""Versioned prompt registry (ESD §20).

Prompts are the *actual program* an LLM agent runs, so they get the same treatment as code:
a stable id, an explicit version, a declared input contract, and a content fingerprint. The
previous design built prompts as inline f-strings, which made three things impossible:

* **Attribution.** An agent step recorded which *model* produced it but not which *prompt*.
  When output quality moved, there was no way to tell whether the model, the evidence, or an
  edited prompt caused it.
* **Evaluation.** A regression harness has to compare like with like. Without a version, an
  eval score cannot be tied to the prompt that produced it.
* **Review.** A prompt embedded in control flow gets edited casually. One in a registry with a
  version bump is a visible change.

``fingerprint`` is derived from the rendered content, so editing a template without bumping
``version`` is detectable rather than silent — the registry check surfaces the drift instead
of trusting the author to remember.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from string import Formatter


class PromptRenderError(RuntimeError):
    """A prompt was rendered with missing or unexpected variables."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """One versioned prompt template."""

    id: str
    version: str
    # Kept separate because the roles differ: `system` states the agent's standing contract
    # and is identical across every call, while `template` carries per-incident content.
    # Separating them also lets the provider place them in distinct message roles, which
    # models weight differently than one concatenated blob.
    system: str
    template: str
    description: str = ""
    _fields: frozenset[str] = field(default_factory=frozenset, compare=False)

    def __post_init__(self) -> None:
        declared = frozenset(name for _, name, _, _ in Formatter().parse(self.template) if name)
        object.__setattr__(self, "_fields", declared)

    @property
    def fingerprint(self) -> str:
        """Content hash. Detects a template edited without a version bump."""
        return hashlib.sha256(f"{self.system}\x00{self.template}".encode()).hexdigest()[:12]

    @property
    def ref(self) -> str:
        """Stable identifier recorded on every agent step: ``id@version+fingerprint``."""
        return f"{self.id}@{self.version}+{self.fingerprint}"

    def render(self, **values: object) -> str:
        """Render the template, failing loudly on a contract mismatch.

        A missing variable would otherwise render as the literal text ``{service}`` and be
        sent to the model, which produces a plausible answer to a malformed question — the
        worst possible failure mode here, because nothing downstream can detect it.
        """
        missing = self._fields - values.keys()
        if missing:
            raise PromptRenderError(
                f"prompt {self.ref} is missing required variable(s): {sorted(missing)}"
            )
        try:
            return self.template.format(**values)
        except (IndexError, KeyError) as exc:  # positional/nested braces in the template
            raise PromptRenderError(f"prompt {self.ref} failed to render: {exc}") from exc


class PromptRegistry:
    """Process-wide prompt lookup."""

    def __init__(self) -> None:
        self._prompts: dict[str, Prompt] = {}

    def register(self, prompt: Prompt) -> Prompt:
        if prompt.id in self._prompts:
            raise ValueError(f"duplicate prompt id: {prompt.id}")
        self._prompts[prompt.id] = prompt
        return prompt

    def get(self, prompt_id: str) -> Prompt:
        try:
            return self._prompts[prompt_id]
        except KeyError:
            raise KeyError(
                f"unknown prompt '{prompt_id}'; registered: {sorted(self._prompts)}"
            ) from None

    def all(self) -> list[Prompt]:
        return [self._prompts[k] for k in sorted(self._prompts)]


REGISTRY = PromptRegistry()
