"""Unit tests: the `escalated` incident state (ESD §6.1, migration 0009).

Escalation existed as a routing decision long before it existed as a state: the supervisor
could choose `escalate`, the graph recorded an agent step, and the incident was then left in
`hypothesis_formed` — identical to one still being actively investigated. The dashboard
therefore could not answer the only question that matters during an outage, which is which
incidents are waiting on a human.

The tests below pin the two properties that make the state useful rather than decorative:
it is reachable from the states an investigation can actually escalate from, and it is
terminal for *automation* without being terminal for the *incident*.
"""

from __future__ import annotations

import pytest

from db.enums import IncidentState
from db.state_machine import (
    IllegalTransitionError,
    assert_legal_transition,
    is_legal_transition,
    legal_next_states,
)

S = IncidentState


def test_escalated_is_a_distinct_state():
    """The whole point: it cannot be confused with an in-flight investigation."""
    assert S.escalated != S.hypothesis_formed
    assert S.escalated.value == "escalated"


@pytest.mark.parametrize("origin", [S.investigating, S.hypothesis_formed])
def test_escalation_is_reachable_from_where_investigations_actually_stop(origin):
    """An investigation escalates either before forming a hypothesis or after one is
    rejected; both must be legal or the transition would raise at exactly the moment the
    system had already given up."""
    assert is_legal_transition(origin, S.escalated) is True


@pytest.mark.parametrize(
    "origin",
    [S.open, S.remediation_proposed, S.remediation_approved, S.monitoring, S.resolved, S.closed],
)
def test_escalation_is_not_reachable_from_unrelated_states(origin):
    """Notably not from `remediation_proposed`: an incident with a proposal awaiting human
    approval is already with a human, and overwriting it would discard that pending
    approval."""
    assert is_legal_transition(origin, S.escalated) is False


def test_escalated_incident_can_still_be_resolved_and_closed():
    """Terminal for automation, not for the incident — a human still finishes it."""
    assert is_legal_transition(S.escalated, S.resolved) is True
    assert is_legal_transition(S.escalated, S.closed) is True


def test_escalated_incident_can_be_handed_back_for_another_pass():
    """The usual reason an investigation escalates is missing evidence. Once a human supplies
    it, retrying must not require first marking the incident resolved — that would record a
    resolution that never happened."""
    assert is_legal_transition(S.escalated, S.investigating) is True


def test_escalated_cannot_jump_straight_to_remediation():
    """Escalation means the evidence did not support a conclusion; proposing a remediation
    from that state would act on the hypothesis the system just declined to stand behind."""
    for target in (S.remediation_proposed, S.remediation_approved, S.remediation_executed):
        assert is_legal_transition(S.escalated, target) is False


def test_illegal_escalation_raises_with_both_states_named():
    with pytest.raises(IllegalTransitionError) as exc:
        assert_legal_transition(S.resolved, S.escalated)
    assert exc.value.from_state is S.resolved
    assert exc.value.to_state is S.escalated


def test_every_state_including_escalated_is_covered_by_the_allow_list():
    """A state absent from the map silently has no legal transitions, which would strand any
    incident that reached it."""
    for state in IncidentState:
        assert legal_next_states(state) is not None


def test_escalated_appears_in_next_states_of_its_origins():
    assert S.escalated in legal_next_states(S.investigating)
    assert S.escalated in legal_next_states(S.hypothesis_formed)
