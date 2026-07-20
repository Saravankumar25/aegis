"""Structured logging for Aegis (ESD §13).

Every incident-scoped log line carries ``incident_id`` as a correlation key (CLAUDE.md §17), so a
single incident's story can be reconstructed across every layer. Uses ``structlog`` with JSON
output; no ``print()`` is used anywhere in application code (CLAUDE.md §3).
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: int = logging.INFO) -> None:
    """Configure structlog + stdlib logging once at process start."""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(incident_id: str | None = None, **initial_values: object) -> structlog.BoundLogger:
    """Return a bound logger, pre-bound with ``incident_id`` when the caller has one.

    Passing the incident id here (rather than on every call) makes it structurally hard to emit an
    incident-scoped log line without the correlation key — the bug CLAUDE.md §17 warns against.
    """
    logger = structlog.get_logger()
    if incident_id is not None:
        logger = logger.bind(incident_id=incident_id)
    if initial_values:
        logger = logger.bind(**initial_values)
    return logger
