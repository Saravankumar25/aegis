"""FastAPI application factory (ESD §4, §7).

The API owns HTTP concerns, auth, and ingestion; it never runs agent reasoning inline —
ingestion commits an ``open`` incident row and the NOTIFY doubles as the worker wake-up.
Run locally:  uvicorn api.main:app --port 8000
"""

from __future__ import annotations

import re
import secrets
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.routing import Match

from api.deps import get_current_user
from api.errors import error_response, install_error_handlers
from api.events import hub
from api.routes import actions, auth, incidents, memory, runbooks
from core.config import get_settings
from core.db import session_scope
from core.logging import configure_logging, get_logger
from core.redis import check_rate_limit, close_redis, redis_stats
from core.redis import ping as redis_ping
from db.models import AgentStep, Incident, User

_ID_SEGMENT = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|\d+|[0-9a-fA-F]{16,})$"
)


def _flatten_routes(routes: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Every leaf route paired with the URL prefix it was mounted under.

    FastAPI does not keep ``include_router`` results as plain ``Route`` objects — 0.139 wraps
    each in a private ``_IncludedRouter`` that exposes neither ``.path`` nor ``.routes``, only
    ``.original_router`` plus an ``include_context`` holding the prefix. The wrapped routes
    therefore carry ``/auth/session``, not ``/api/v1/auth/session``, so matching them against a
    real request path silently fails. Anything unrecognised is skipped rather than guessed at.
    """
    flat: list[tuple[str, Any]] = []
    for route in routes or []:
        if hasattr(route, "path"):
            flat.append((prefix, route))
            continue
        context = getattr(route, "include_context", None)
        inner = getattr(route, "original_router", None)
        if inner is not None:
            flat.extend(
                _flatten_routes(
                    getattr(inner, "routes", None), prefix + getattr(context, "prefix", "")
                )
            )
        elif hasattr(route, "routes"):
            flat.extend(_flatten_routes(route.routes, prefix))
    return flat


def _normalized_path(path: str) -> str:
    """Framework-independent fallback: collapse identifier-shaped segments.

    Used when the routing table yields no match, so that a future FastAPI internal change
    cannot quietly restore the per-URL-budget bypass. It is a backstop, not the primary
    mechanism: it recognises the *shape* of an id rather than knowing the route.
    """
    return "/".join("{id}" if _ID_SEGMENT.match(seg) else seg for seg in path.split("/"))


def route_template(request: Request) -> str:
    """The matched route's *template*, resolved before routing has run.

    ``@app.middleware("http")`` executes outside the router, so ``request.scope["route"]`` is
    not populated yet — reading it always yields ``None``. The rate limiter did exactly that
    and silently fell back to the concrete path, which turned the limit into a per-URL budget
    rather than a per-route one: any caller who varied the id in a parameterised path got a
    fresh allowance for every URL and was never limited at all (measured: 200 requests to
    ``/api/v1/incidents/<uuid>`` with a fresh uuid each time produced zero 429s).

    Matching against the routing table here restores the intended bucket. Requests matching no
    route fall back to identifier-shape normalisation, so spraying random 404s cannot be used
    to evade the limit either.
    """
    path = request.url.path
    for prefix, route in _flatten_routes(request.app.routes):
        if prefix and not path.startswith(prefix):
            continue
        scope = {**request.scope, "path": path[len(prefix) :] or "/"}
        match, _ = route.matches(scope)
        if match is not Match.NONE:
            # PARTIAL (path matched, method did not) still identifies the template, and the
            # method is already a separate component of the bucket key.
            return prefix + str(route.path)
    return _normalized_path(path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await hub.start()
    get_logger(component="api").info("api_started")
    yield
    await hub.stop()
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    # The interactive docs enumerate every route, its schema, and its auth requirements —
    # a complete map of the attack surface, served unauthenticated. They stay on outside
    # production because they are genuinely useful while developing, and are switched off by
    # environment rather than by a flag someone has to remember to set (ESD §16).
    docs_enabled = settings.environment.lower() not in {"production", "prod"}
    app = FastAPI(
        title="Aegis API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    install_error_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_settings().cors_origins.split(","),
        allow_credentials=True,  # httpOnly cookies ride on credentialed requests
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        """Per-client request cap (ESD §16). Fails open if Redis is down — see core.redis.

        The bucket key is the client IP plus the route *template*, not the concrete path, so
        one incident's detail requests cannot exhaust the budget for every other incident.
        SSE streams are exempt: they are long-lived by design and counting them would evict a
        dashboard that is behaving exactly as intended.
        """
        settings = get_settings()
        path = request.url.path
        if request.method == "OPTIONS" or path.endswith("/stream") or path.endswith("/health"):
            return await call_next(request)

        is_ingest = path == "/api/v1/incidents" and request.method == "POST"
        limit = (
            settings.ingest_rate_limit_per_window
            if is_ingest
            else settings.api_rate_limit_per_window
        )
        client_ip = request.client.host if request.client else "unknown"
        scope_key = route_template(request)
        decision = await check_rate_limit(
            f"{client_ip}:{request.method}:{scope_key}",
            limit=limit,
            window_seconds=settings.api_rate_limit_window_seconds,
        )
        if not decision.allowed:
            get_logger(component="api").warning(
                "rate_limited", client_ip=client_ip, path=path, limit=limit
            )
            return error_response(
                "rate_limited",
                "too many requests; retry shortly",
                status_code=429,
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        return response

    @app.middleware("http")
    async def webhook_guard(request: Request, call_next):
        """Shared-secret check on the ingestion webhook when a token is configured."""
        settings = get_settings()
        if (
            settings.ingest_webhook_token
            and request.url.path == "/api/v1/incidents"
            and request.method == "POST"
        ):
            # compare_digest, not ``!=``: a plain comparison short-circuits on the first
            # differing byte, so the response time leaks how much of the token a caller has
            # guessed and the secret can be recovered a byte at a time. The header is
            # coerced to str because compare_digest rejects None.
            presented = request.headers.get("x-aegis-webhook-token") or ""
            if not secrets.compare_digest(presented, settings.ingest_webhook_token):
                # Returned, not raised — see error_response(): a raise would surface as 500.
                return error_response("webhook_unauthorized", "bad webhook token", status_code=401)
        return await call_next(request)

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(incidents.router, prefix="/api/v1")
    app.include_router(runbooks.router, prefix="/api/v1")
    app.include_router(actions.router, prefix="/api/v1")
    app.include_router(memory.router, prefix="/api/v1")

    # Registered at both paths deliberately. `/health` is what infrastructure probes —
    # Kubernetes probes, load balancers, uptime checks — expect, and it is the path ESD §7
    # documents; a probe hitting it got a 404 while the endpoint sat under `/api/v1`, which
    # would have read as a dead instance on the first real deployment. The versioned path is
    # kept because the dashboard already calls it, and an unversioned operational endpoint
    # should not be forced through API versioning anyway.
    @app.get("/health")
    @app.get("/api/v1/health")
    async def health(response: Response) -> dict[str, Any]:
        """Dependency health.

        The status distinguishes the two dependencies by their role rather than treating
        every check alike. Postgres is the source of truth, so losing it means the API
        cannot serve correct answers → 503, and a load balancer should pull this instance.
        Redis is a cache that every call site degrades around, so losing it is `degraded`
        and still 200: taking the instance out of rotation over a cache outage would cause
        the outage it is meant to prevent.
        """
        checks: dict[str, Any] = {}
        try:
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except SQLAlchemyError as exc:
            get_logger(component="api").error("health_postgres_failed", error=str(exc))
            checks["postgres"] = "unreachable"
        checks["redis"] = "ok" if await redis_ping() else "unreachable"

        if checks["postgres"] != "ok":
            response.status_code = 503
            status = "unhealthy"
        elif checks["redis"] != "ok":
            status = "degraded"
        else:
            status = "ok"
        return {"status": status, "checks": checks}

    @app.get("/metrics")
    @app.get("/api/v1/metrics")
    async def metrics(_user: User = Depends(get_current_user)) -> dict[str, Any]:
        """Operational counters (ESD §13).

        Authenticated at both paths — deliberately, even though `/metrics` is conventionally
        the scrape endpoint. Incident and token counts describe production activity and the
        Redis keyspace reveals usage patterns, so this stays behind auth and a scraper is
        given credentials, rather than the endpoint being opened to match convention.
        """
        async with session_scope() as session:
            incidents_by_state = {
                str(state): count
                for state, count in (
                    await session.execute(
                        select(Incident.state, func.count()).group_by(Incident.state)
                    )
                ).all()
            }
            llm = (
                await session.execute(
                    select(
                        func.count(AgentStep.id),
                        func.coalesce(func.sum(AgentStep.tokens_used), 0),
                        func.coalesce(func.sum(AgentStep.cost_usd), 0),
                        func.avg(AgentStep.latency_ms),
                    )
                )
            ).one()
        return {
            "incidents_by_state": incidents_by_state,
            "llm": {
                "agent_steps": llm[0],
                "tokens_used_total": int(llm[1]),
                "cost_usd_total": float(llm[2]),
                "mean_latency_ms": round(float(llm[3]), 1) if llm[3] is not None else None,
            },
            "redis": await redis_stats(),
        }

    return app


app = create_app()
