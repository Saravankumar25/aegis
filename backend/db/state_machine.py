"""Incident state machine (ESD §6.1).

Every incident transition is validated against an explicit allow-list of legal ``(from_state,
to_state)`` pairs *at the application layer*, in addition to the native Postgres enum that limits
``incidents.state`` to the defined values. This module is pure logic (no I/O), so the transition
rules are unit-tested in isolation from the database (CLAUDE.md §7).
"""

from __future__ import annotations

from db.enums import IncidentState

S = IncidentState

# Legal transitions. The remediation_* edges are V1.5 (Resolution Agent); the
# hypothesis_formed→{monitoring,resolved} edges are the MVP investigation-only paths, where a human
# acts on the hypothesis without any automated remediation. Both are recorded in ESD §6.1.
LEGAL_TRANSITIONS: dict[IncidentState, frozenset[IncidentState]] = {
    S.open: frozenset({S.investigating}),
    S.investigating: frozenset({S.hypothesis_formed}),
    S.hypothesis_formed: frozenset(
        {S.remediation_proposed, S.remediation_executed, S.monitoring, S.resolved}
    ),
    S.remediation_proposed: frozenset({S.remediation_approved}),
    S.remediation_approved: frozenset({S.remediation_executed}),
    S.remediation_executed: frozenset({S.monitoring}),
    S.monitoring: frozenset({S.resolved}),
    S.resolved: frozenset({S.closed, S.reopened}),
    S.closed: frozenset({S.reopened}),
    S.reopened: frozenset({S.investigating}),
}


class IllegalTransitionError(ValueError):
    """Raised when a ``(from_state, to_state)`` pair is not in the allow-list."""

    def __init__(self, from_state: IncidentState, to_state: IncidentState) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(f"illegal incident transition: {from_state.value} -> {to_state.value}")


def is_legal_transition(from_state: IncidentState, to_state: IncidentState) -> bool:
    """Return True iff moving from ``from_state`` to ``to_state`` is allowed."""
    return to_state in LEGAL_TRANSITIONS.get(from_state, frozenset())


def assert_legal_transition(from_state: IncidentState, to_state: IncidentState) -> None:
    """Raise :class:`IllegalTransitionError` unless the transition is legal.

    Callers use this before writing an ``incident_state_transitions`` row so an invalid lifecycle
    move is rejected deterministically rather than silently persisted.
    """
    if not is_legal_transition(from_state, to_state):
        raise IllegalTransitionError(from_state, to_state)


def legal_next_states(from_state: IncidentState) -> frozenset[IncidentState]:
    """Return the set of states reachable from ``from_state`` in one legal step."""
    return LEGAL_TRANSITIONS.get(from_state, frozenset())
