"""Meridian Commerce simulated microservice (Aegis local chaos environment, ESD §18).

One image, parameterised by ``SERVICE_NAME``, stands in for each Meridian service (checkout,
payment, catalog, ...). It exposes Prometheus metrics and an admin endpoint to inject failures, so a
realistic incident (error-rate spike or latency regression) can be generated on demand and observed
by the Correlation Agent through Prometheus.

A background loop continuously simulates request traffic and applies the current failure mode, so the
metrics reflect the injected fault even without external load. This is deliberately a *simulator*: it
is the target environment Aegis investigates, not part of Aegis itself.

Simulated traffic is **logged as well as counted**. Originally it only incremented Prometheus
counters, which made the environment emit self-contradicting evidence: Prometheus reported a 31%
error rate while `kubectl logs` showed nothing but healthy `/health` probes. Aegis's entire
log-evidence path — fetch, redact, delimit, cite, ground a hypothesis on the citation — was
therefore exercised against logs that could not possibly contain a failure, and the Observer
correctly refused to approve any hypothesis about an error spike it could see no log evidence for.
An investigation target whose signals disagree tests nothing except the refusal path.

Error logs carry a plausible downstream cause rather than a bare "500", because the point of the
environment is to give Correlation and RCA a real thread to pull: checkout failing *because* its
payment dependency is timing out is an incident with a root cause, whereas a naked status code is
a fact with nowhere to go.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel

SERVICE_NAME = os.environ.get("SERVICE_NAME", "unknown-service")
# Baseline synthetic requests per scrape loop tick, and tick interval.
BASE_RPS = float(os.environ.get("BASE_RPS", "20"))
TICK_SECONDS = float(os.environ.get("TICK_SECONDS", "1.0"))

REQUESTS = Counter(
    "http_requests_total", "Total HTTP requests", ["service", "endpoint", "status"]
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "endpoint"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
UP = Gauge("app_up", "1 if the service considers itself healthy", ["service"])
ERROR_RATE = Gauge(
    "app_injected_error_rate", "Currently injected error rate 0..1", ["service"]
)


class FailureMode(BaseModel):
    """Injected failure configuration (set via ``POST /admin/failure``)."""

    mode: str = "none"  # none | error | latency
    rate: float = 0.0  # fraction of simulated requests affected, 0..1
    latency_ms: int = 800  # added latency when mode == "latency"


_state = FailureMode()


log = logging.getLogger("meridian")

# Successful requests are sampled: a real service does not log every 200 at 20 rps, and a log
# tail full of successes would bury the failures an investigator needs to see.
SUCCESS_LOG_SAMPLE = float(os.environ.get("SUCCESS_LOG_SAMPLE", "0.02"))

# The dependency each service blames when it fails, so a multi-service incident has a direction
# for Correlation to follow rather than three services all reporting unrelated errors.
_UPSTREAM = {
    "checkout-service": "payment-service",
    "payment-service": "catalog-service",
}


def _log_error(endpoint: str, request_id: str, latency: float) -> None:
    """Emit an application error log resembling a real framework's output."""
    upstream = _UPSTREAM.get(SERVICE_NAME)
    if upstream:
        log.error(
            "request_id=%s %s %s -> 500 upstream=%s "
            "error=UpstreamTimeout: connection to %s timed out after %dms "
            "(pool exhausted, 0 idle connections)",
            request_id,
            "POST",
            endpoint,
            upstream,
            upstream,
            int(latency * 1000),
        )
    else:
        log.error(
            "request_id=%s %s %s -> 500 error=InternalError: "
            "unhandled exception while serving request (duration=%dms)",
            request_id,
            "GET",
            endpoint,
            int(latency * 1000),
        )


def _simulate_one(endpoint: str) -> None:
    """Simulate a single request, recording metrics and logs per the active failure mode."""
    base_latency = random.uniform(0.02, 0.12)
    affected = random.random() < _state.rate
    status = "200"
    latency = base_latency
    if affected and _state.mode == "error":
        status = "500"
    elif affected and _state.mode == "latency":
        latency = base_latency + _state.latency_ms / 1000.0
    REQUESTS.labels(SERVICE_NAME, endpoint, status).inc()
    LATENCY.labels(SERVICE_NAME, endpoint).observe(latency)

    request_id = uuid.uuid4().hex[:12]
    if status == "500":
        _log_error(endpoint, request_id, latency)
    elif affected and _state.mode == "latency":
        log.warning(
            "request_id=%s GET %s -> 200 slow_request duration=%dms threshold=500ms",
            request_id,
            endpoint,
            int(latency * 1000),
        )
    elif random.random() < SUCCESS_LOG_SAMPLE:
        log.info(
            "request_id=%s GET %s -> 200 duration=%dms",
            request_id,
            endpoint,
            int(latency * 1000),
        )


async def _traffic_loop() -> None:
    """Continuously generate synthetic traffic so metrics reflect the injected mode."""
    endpoints = ["/", "/checkout", "/api/orders"]
    while True:
        for _ in range(int(BASE_RPS)):
            _simulate_one(random.choice(endpoints))
        UP.labels(SERVICE_NAME).set(1)
        ERROR_RATE.labels(SERVICE_NAME).set(_state.rate if _state.mode == "error" else 0.0)
        await asyncio.sleep(TICK_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configured explicitly rather than relying on uvicorn's setup: uvicorn configures its own
    # loggers only, so a bare `logging.getLogger("meridian")` inherits the root logger's default
    # WARNING level and silently drops every INFO line — which is precisely the failure this
    # module's docstring describes, one layer down.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        force=True,
    )
    task = asyncio.create_task(_traffic_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title=f"meridian-{SERVICE_NAME}", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"service": SERVICE_NAME, "status": "ok"}


@app.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/admin/failure")
async def set_failure(mode: FailureMode) -> dict[str, object]:
    """Inject or clear a failure mode. Used by the fault-injection scripts (ESD §18)."""
    global _state
    _state = mode
    start = time.time()
    return {
        "service": SERVICE_NAME,
        "applied": _state.model_dump(),
        "at": start,
    }


@app.get("/admin/failure")
async def get_failure() -> dict[str, object]:
    return {"service": SERVICE_NAME, "current": _state.model_dump()}
