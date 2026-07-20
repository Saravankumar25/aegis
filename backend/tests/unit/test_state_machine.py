"""Unit tests for the incident state-machine allow-list (ESD §6.1, CLAUDE.md §7).

Pure-logic tests — no database — proving that legal transitions are accepted and illegal ones are
rejected deterministically.
"""

from __future__ import annotations

import pytest

from db.enums import IncidentState as S
from db.state_machine import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    assert_legal_transition,
    is_legal_transition,
    legal_next_states,
)

# The MVP investigation-only happy path plus a reopen cycle.
MVP_HAPPY_PATH = [
    (S.open, S.investigating),
    (S.investigating, S.hypothesis_formed),
    (S.hypothesis_formed, S.resolved),
    (S.resolved, S.closed),
    (S.closed, S.reopened),
    (S.reopened, S.investigating),
]

# Representative illegal moves that must never be allow-listed.
ILLEGAL_TRANSITIONS = [
    (S.open, S.resolved),  # cannot skip investigation
    (S.open, S.hypothesis_formed),
    (S.investigating, S.open),  # no going backward to open
    (S.resolved, S.investigating),  # must reopen first
    (S.hypothesis_formed, S.open),
    (S.closed, S.resolved),
    (S.monitoring, S.hypothesis_formed),
]


@pytest.mark.parametrize(("frm", "to"), MVP_HAPPY_PATH)
def test_legal_transitions_accepted(frm: S, to: S) -> None:
    assert is_legal_transition(frm, to) is True
    assert_legal_transition(frm, to)  # must not raise


@pytest.mark.parametrize(("frm", "to"), ILLEGAL_TRANSITIONS)
def test_illegal_transitions_rejected(frm: S, to: S) -> None:
    assert is_legal_transition(frm, to) is False
    with pytest.raises(IllegalTransitionError) as exc:
        assert_legal_transition(frm, to)
    assert exc.value.from_state is frm
    assert exc.value.to_state is to


def test_v15_remediation_path_is_allow_listed() -> None:
    """The V1.5 remediation chain is present so the schema is stable across phases (ESD §6.1)."""
    chain = [
        (S.hypothesis_formed, S.remediation_proposed),
        (S.remediation_proposed, S.remediation_approved),
        (S.remediation_approved, S.remediation_executed),
        (S.remediation_executed, S.monitoring),
        (S.monitoring, S.resolved),
    ]
    for frm, to in chain:
        assert is_legal_transition(frm, to), f"{frm} -> {to} should be legal"


def test_tier1_shortcut_hypothesis_to_executed() -> None:
    """Tier-1 auto-approve reaches remediation_executed directly from hypothesis_formed."""
    assert is_legal_transition(S.hypothesis_formed, S.remediation_executed)


def test_every_state_is_a_known_key_or_reachable() -> None:
    """Every enum state is either a source in the map or a reachable target (no orphans)."""
    sources = set(LEGAL_TRANSITIONS.keys())
    targets = {t for tos in LEGAL_TRANSITIONS.values() for t in tos}
    for state in S:
        assert state in sources or state in targets, f"{state} is unreachable and has no exits"


def test_transitions_target_only_defined_states() -> None:
    """No transition points at a state outside the enum."""
    valid = set(S)
    for tos in LEGAL_TRANSITIONS.values():
        assert tos <= valid


def test_legal_next_states_matches_map() -> None:
    assert legal_next_states(S.resolved) == frozenset({S.closed, S.reopened})
    assert legal_next_states(S.open) == frozenset({S.investigating})
