"""Unit tests: deterministic severity classification (PRD FR-1.3, CLAUDE.md §7)."""

from __future__ import annotations

import pytest

from agents.triage.classifier import classify_severity
from db.enums import Severity


@pytest.mark.parametrize(
    ("service", "kind", "expected"),
    [
        ("checkout-service", "error_rate", Severity.P1),
        ("payment-service", "availability", Severity.P1),
        ("payment-service", "pod_crash", Severity.P1),
        ("checkout-service", "latency", Severity.P2),
        ("checkout-service", "other", Severity.P3),
        ("catalog-service", "error_rate", Severity.P2),
        ("catalog-service", "latency", Severity.P3),
        ("catalog-service", "other", Severity.P4),
        ("mystery-service", "error_rate", Severity.P2),  # unknown ≠ buried, ≠ P1-paged
        ("mystery-service", "latency", Severity.P3),
        ("mystery-service", "other", Severity.P4),
    ],
)
def test_severity_matrix(service: str, kind: str, expected: Severity):
    assert classify_severity(service, kind) == expected


def test_kind_is_case_insensitive():
    assert classify_severity("checkout-service", "ERROR_RATE") == Severity.P1
