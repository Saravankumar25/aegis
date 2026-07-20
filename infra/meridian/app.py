"""Meridian Commerce simulated microservice (Aegis local chaos environment, ESD §18).

One image, parameterised by ``SERVICE_NAME``, stands in for each Meridian service (checkout,
payment, catalog, ...). It exposes Prometheus metrics and an admin endpoint to inject failures, so a
realistic incident (error-rate spike or latency regression) can be generated on demand and observed
by the Correlation Agent through Prometheus.

A background loop continuously simulates request traffic and applies the current failure mode, so the
metrics reflect the injected fault even without external load. This is deliberately a *simulator*: it
is the target environment Aegis investigates, not part of Aegis itself.
"""

from __future__ import annotations

import asyncio
import os
import random
import time
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


def _simulate_one(endpoint: str) -> None:
    """Simulate a single request, recording metrics per the active failure mode."""
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
