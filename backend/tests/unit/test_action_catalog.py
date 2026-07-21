"""Unit tests: action catalog invariants (FR-4.1) and category mapping."""

from __future__ import annotations

from agents.resolution.actions import CATALOG, CATEGORY_ACTION, recommend_action
from agents.resolution.engine import estimate_blast_radius


def test_every_action_is_pre_classified_with_documented_undo():
    for spec in CATALOG.values():
        assert spec.tier in (1, 2, 3)  # FR-4.1: pre-classified, no unclassified actions
        assert spec.compensating.get("note"), "every action documents its undo (CLAUDE.md §17)"


def test_tier3_actions_have_no_machine_execution_path():
    tier3 = [s for s in CATALOG.values() if s.tier == 3]
    assert tier3, "catalog must demonstrate a Tier-3 (human-only) action"
    for spec in tier3:
        assert spec.mcp_server is None and spec.mcp_tool is None


def test_category_mapping_only_references_cataloged_actions():
    for action_type in CATEGORY_ACTION.values():
        assert action_type is None or action_type in CATALOG


def test_error_spike_and_unknown_recommend_nothing():
    assert recommend_action("error_spike") is None
    assert recommend_action("unknown") is None
    assert recommend_action("never-heard-of-it") is None


def test_blast_radius_uses_topology():
    radius = estimate_blast_radius("payment-service")
    assert radius["dependents"] == ["checkout-service"]  # checkout calls payment
    assert radius["count"] == 1
    assert estimate_blast_radius("checkout-service")["count"] == 0  # nobody calls checkout
