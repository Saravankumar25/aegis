"""Meridian Commerce service topology (ESD §18) — the topological correlation dimension.

MVP keeps this as an explicit map: three services, known dependencies. The Correlation
Agent uses it to scope evidence (FR-2.3); V1.5 blast-radius estimation (FR-4.4) reuses it.
"""

from __future__ import annotations

# service -> services it calls
DEPENDENCIES: dict[str, list[str]] = {
    "checkout-service": ["payment-service", "catalog-service"],
    "payment-service": [],
    "catalog-service": [],
}


def dependencies_of(service: str) -> list[str]:
    """Services the given service calls (its failure suspects)."""
    return DEPENDENCIES.get(service, [])


def dependents_of(service: str) -> list[str]:
    """Services that call the given service (its blast radius, FR-4.4)."""
    return [s for s, deps in DEPENDENCIES.items() if service in deps]
