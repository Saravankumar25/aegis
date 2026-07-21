"""Pydantic models for the Prometheus MCP server's tool outputs (typed boundaries).

Shapes mirror the Prometheus HTTP API v1 result types, trimmed to what the Correlation and
RCA agents consume as metric evidence (FR-2.1). Alert annotations/labels can contain
operator-authored free text and are treated as untrusted (ESD §16).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class InstantSample(BaseModel):
    """One series of an instant-query vector result."""

    metric: dict[str, str] = Field(default_factory=dict)
    timestamp: float
    value: str  # Prometheus returns sample values as strings; kept verbatim


class RangeSeries(BaseModel):
    """One series of a range-query matrix result."""

    metric: dict[str, str] = Field(default_factory=dict)
    values: list[tuple[float, str]] = Field(default_factory=list)


class InstantQueryResult(BaseModel):
    """`query_metrics` output."""

    query: str
    result_type: str  # vector | scalar
    samples: list[InstantSample] = Field(default_factory=list)


class RangeQueryResult(BaseModel):
    """`query_range_metrics` output."""

    query: str
    start: str
    end: str
    step: str
    series: list[RangeSeries] = Field(default_factory=list)


class AlertSummary(BaseModel):
    """One active alert from `list_alerts`. Label/annotation values are untrusted text."""

    name: str
    state: str  # firing | pending
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    active_at: str | None = None
    value: str | None = None
