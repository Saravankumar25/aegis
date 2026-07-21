"""Unit tests: action catalog invariants (FR-4.1).

The category→action mapping these tests once covered has been deleted: action selection is
now the Resolution agent's reasoning (`agents/resolution/planner.py`), and a static mapping
kept alongside it would be a second, silently diverging source of truth. What survives here
are the invariants the *catalog* must hold regardless of who selects from it — those are
what the planner's allowlist and the engine's tiering both depend on.
"""

from __future__ import annotations

from agents.resolution.actions import CATALOG
from agents.resolution.engine import estimate_blast_radius
from agents.resolution.planner import PARAMETER_BOUNDS


def test_every_action_is_pre_classified_with_documented_undo():
    for spec in CATALOG.values():
        assert spec.tier in (1, 2, 3)  # FR-4.1: pre-classified, no unclassified actions
        assert spec.compensating.get("note"), "every action documents its undo (CLAUDE.md §17)"


def test_tier3_actions_have_no_machine_execution_path():
    tier3 = [s for s in CATALOG.values() if s.tier == 3]
    assert tier3, "catalog must demonstrate a Tier-3 (human-only) action"
    for spec in tier3:
        assert spec.mcp_server is None and spec.mcp_tool is None


def test_catalog_keys_match_their_action_type():
    """The planner looks specs up by the key the model names; a mismatch would execute
    one action under another's tier and compensating action."""
    for key, spec in CATALOG.items():
        assert key == spec.action_type


def test_every_tunable_parameter_has_declared_bounds():
    """A parameter the model can set but Python cannot clamp is an unbounded instruction."""
    assert "replicas_delta" in PARAMETER_BOUNDS
    for name, (low, high) in PARAMETER_BOUNDS.items():
        assert low <= high, name
        assert low >= 1, f"{name} lower bound must be a real change, not a no-op"


def test_blast_radius_uses_topology():
    radius = estimate_blast_radius("payment-service")
    assert radius["dependents"] == ["checkout-service"]  # checkout calls payment
    assert radius["count"] == 1
    assert estimate_blast_radius("checkout-service")["count"] == 0  # nobody calls checkout
