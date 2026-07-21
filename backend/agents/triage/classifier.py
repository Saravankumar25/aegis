"""Deterministic severity classification (PRD FR-1.3) — the Triage Agent's rule core.

Pure logic, no I/O: severity derives from the affected service's criticality tier and the
alert kind. Kept separate from the agent node so it is unit-testable in isolation
(CLAUDE.md §7) and reusable by the ingestion endpoint, which must assign a provisional
severity synchronously (an incident row is created within 1s of the webhook, PRD 11A).
"""

from __future__ import annotations

from db.enums import Severity

# Meridian Commerce service criticality (ESD §18 topology). Revenue-path services are
# critical; catalog is important but degradable.
CRITICAL_SERVICES = {"checkout-service", "payment-service"}
IMPORTANT_SERVICES = {"catalog-service"}

# Alert kinds, roughly ordered by operational urgency.
_URGENT_KINDS = {"availability", "pod_crash", "error_rate"}
_DEGRADED_KINDS = {"latency"}


def classify_severity(service_name: str, kind: str) -> Severity:
    """Map (service criticality × alert kind) → P1-P4.

    Unknown services are treated as important-tier rather than critical: a missing entry in
    the criticality map should not silently page at P1, but must not be buried at P4 either.
    """
    kind = kind.lower()
    if service_name in CRITICAL_SERVICES:
        if kind in _URGENT_KINDS:
            return Severity.P1
        if kind in _DEGRADED_KINDS:
            return Severity.P2
        return Severity.P3
    if service_name in IMPORTANT_SERVICES:
        if kind in _URGENT_KINDS:
            return Severity.P2
        if kind in _DEGRADED_KINDS:
            return Severity.P3
        return Severity.P4
    # Unknown service: middle course.
    if kind in _URGENT_KINDS:
        return Severity.P2
    if kind in _DEGRADED_KINDS:
        return Severity.P3
    return Severity.P4
