"""FastAPI application factory (ESD §4, §7).

The API owns HTTP concerns, auth, and ingestion; it never runs agent reasoning inline —
ingestion commits an ``open`` incident row and the NOTIFY doubles as the worker wake-up.
Run locally:  uvicorn api.main:app --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await hub.start()
    get_logger(component="api").info("api_started")
    yield
    await hub.stop()
    await close_redis()


def create_app() -> FastAPI:
    app = FastAPI(title="Aegis API", version="0.1.0", lifespan=lifespan)
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
        route = request.scope.get("route")
        scope_key = getattr(route, "path", path)
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
            and request.headers.get("x-aegis-webhook-token") != settings.ingest_webhook_token
        ):
            # Returned, not raised — see error_response(): a raise here would surface as 500.
            return error_response("webhook_unauthorized", "bad webhook token", status_code=401)
        return await call_next(request)

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(incidents.router, prefix="/api/v1")
    app.include_router(runbooks.router, prefix="/api/v1")
    app.include_router(actions.router, prefix="/api/v1")
    app.include_router(memory.router, prefix="/api/v1")

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

    @app.get("/api/v1/metrics")
    async def metrics(_user: User = Depends(get_current_user)) -> dict[str, Any]:
        """Operational counters (ESD §13).

        Authenticated: incident and token counts describe production activity and the Redis
        keyspace reveals usage patterns, none of which belongs on an anonymous endpoint.
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
