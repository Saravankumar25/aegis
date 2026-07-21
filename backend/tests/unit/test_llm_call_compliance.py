"""Structural guard: every LLM call goes through the production plumbing (ESD §20, §16).

Enforced by inspecting source rather than by review, because the failure is invisible at
runtime: an ad-hoc `provider.complete("...")` with a hand-built prompt works perfectly, and
only shows up later as an agent whose output cannot be attributed to a prompt version, whose
input was never screened, and whose tokens are unaccounted.

These tests fail on the *addition* of a non-compliant call site, which is the point — the
audit that found RCA bypassing the registry was manual, and manual audits do not run in CI.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
# Directories whose modules may legitimately call a provider.
AGENT_DIRS = ("agents", "orchestrator", "api", "worker", "memory", "rag")

PROVIDER_METHODS = {"complete", "complete_structured", "stream"}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in AGENT_DIRS:
        files.extend((BACKEND / directory).rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def _modules_calling_provider() -> dict[Path, list[str]]:
    """Map each source file to the provider methods it calls."""
    found: dict[Path, list[str]] = {}
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        methods = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in PROVIDER_METHODS
            # `provider.complete(...)` / `services.provider.complete(...)`, not `str.strip()`
            and "provider" in ast.dump(node.func.value).lower()
        ]
        if methods:
            found[path] = methods
    return found


def test_every_llm_call_site_uses_the_prompt_registry():
    """A prompt built inline cannot be versioned, attributed, or evaluated."""
    offenders = []
    for path in _modules_calling_provider():
        source = path.read_text(encoding="utf-8")
        if "prompts.library" not in source:
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == [], (
        f"LLM call sites not using the prompt registry: {offenders}. "
        f"Import the prompt from agents.prompts.library instead of building it inline."
    )


def test_every_llm_call_site_passes_a_prompt_ref():
    """Without prompt_ref the resulting agent_step cannot be attributed to a prompt version."""
    offenders = []
    for path in _modules_calling_provider():
        source = path.read_text(encoding="utf-8")
        if "prompt_ref=" not in source:
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == [], f"LLM call sites not recording prompt_ref: {offenders}"


def test_every_llm_call_site_applies_guardrails():
    """Untrusted evidence reaches these prompts; an unscreened call site is an injection path."""
    offenders = []
    for path in _modules_calling_provider():
        source = path.read_text(encoding="utf-8")
        if "guard_input" not in source and "guard_output" not in source:
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == [], f"LLM call sites without guardrails: {offenders}"


def test_no_module_constructs_a_provider_directly():
    """Providers come from the factory so configuration and fallbacks are honoured."""
    offenders = []
    for path in _python_files():
        source = path.read_text(encoding="utf-8")
        if "OpenRouterProvider(" in source and "factory" not in path.name:
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == [], f"modules constructing a provider directly: {offenders}"


@pytest.mark.parametrize(
    "expected",
    [
        "agents/triage/reasoner.py",
        "agents/correlation/planner.py",
        "agents/rca/engine.py",
        "agents/observer/critic.py",
        "agents/communication/writer.py",
        "orchestrator/supervisor.py",
    ],
)
def test_expected_agents_actually_call_a_model(expected):
    """The inverse guard: an agent that stops calling a model has become fake AI again.

    This is what the audit found — six of seven 'agents' were deterministic Python wearing an
    agent's name. If one of these files stops invoking a provider, that regression is silent
    without this test.
    """
    calling = {str(p.relative_to(BACKEND)).replace("\\", "/") for p in _modules_calling_provider()}
    assert expected in calling, f"{expected} no longer calls an LLM"
