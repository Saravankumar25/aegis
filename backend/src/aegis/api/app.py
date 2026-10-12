"""FastAPI application factory.

Responsibilities kept deliberately narrow: wiring, lifespan, error translation
and middleware. No business logic lives here.

Boot ordering matters. Postgres is the only hard dependency, so it is
established (and migrated) before the app serves traffic. Every soft dependency
is initialised best-effort: a missing Neo4j or an unconfigured Firebase degrades
a feature and is reported by /health, but never prevents startup.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, ORJSONResponse

from aegis.api.metrics import install_metrics
from aegis.api.routers import (
    access,
    actions,
    alerts,
    approvals,
    audit,
    deployments,
    evaluation,
    graph,
    health,
    incidents,
    integrations,
    investigations,
    policy_admin,
    reliability,
    stream,
    systems,
    tasks,
    war_room,
)
from aegis.container import build_container
from aegis.core.config import Settings, get_settings
from aegis.core.errors import AegisError, DatabaseResuming
from aegis.core.ids import correlation_id
from aegis.core.logging import bind_correlation_id, configure_logging, get_logger
from aegis.persistence.migrate import run_migrations

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log.info(
        "aegis api starting",
        version=settings.aegis_version,
        environment=settings.aegis_env.value,
        autonomy_enabled=settings.autonomy_enabled,
        autonomy_mode=settings.autonomy_mode.value,
    )

    # One composition root, shared with the worker. Two processes wiring their
    # own components is how an API ends up enforcing a different policy from
    # the worker that actually executes.
    container = build_container(settings)
    app.state.settings = settings
    app.state.container = container
    app.state.db = container.db
    app.state.incidents = container.incidents
    app.state.evidence = container.evidence
    app.state.redis = container.redis
    # The war room reads checkpoints, events and the brain's readiness; the
    # worker owns the write side of all three.
    app.state.horizon_store = container.horizon_store
    app.state.brain = container.brain

    try:
        await container.connect()
    except DatabaseResuming:
        if settings.api_runtime != "lambda":
            raise
        log.warning("database is resuming; Lambda will retry on the next request")
    if settings.api_run_migrations:
        await run_migrations(container.db)

    from aegis.api.security import FirebaseVerifier

    verifier = FirebaseVerifier(settings)
    verifier.initialise()  # best effort; /health reports the outcome
    app.state.verifier = verifier

    unavailable = [name for name, cap in container.capabilities.items() if not cap.configured]
    log.info(
        "aegis api ready",
        capabilities_available=len(container.capabilities) - len(unavailable),
        capabilities_unavailable=unavailable,
    )

    try:
        yield
    finally:
        log.info("aegis api shutting down")
        await container.aclose()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Aegis",
        version=settings.aegis_version,
        description="Evidence-driven AI SRE control plane",
        default_response_class=ORJSONResponse,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Aegis-Ingest-Token", "Last-Event-ID"],
        max_age=600,
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Give every request a correlation id and echo it back.

        The same id lands in logs, audit rows and the OTel trace, so an operator
        can pivot between them from a single value in a response header.
        """
        cid = request.headers.get("X-Correlation-ID") or correlation_id()
        bind_correlation_id(cid)
        request.state.correlation_id = cid
        try:
            if settings.api_runtime == "lambda" and not request.app.state.db.is_ready:
                try:
                    await request.app.state.container.connect()
                except DatabaseResuming as resume_error:
                    response = _database_resuming_response(request, resume_error, settings)
                else:
                    response = await call_next(request)
            else:
                response = await call_next(request)
        except (
            asyncpg.CannotConnectNowError,
            asyncpg.ConnectionDoesNotExistError,
            asyncpg.PostgresConnectionError,
            asyncpg.TooManyConnectionsError,
            ConnectionError,
            TimeoutError,
        ) as cause:
            database_unavailable = DatabaseResuming(
                "database is resuming; retry after the advertised delay",
                context={"dependency": "postgres"},
            )
            log.warning("database request unavailable", error=type(cause).__name__)
            response = _database_resuming_response(request, database_unavailable, settings)
        finally:
            bind_correlation_id(None)
        response.headers["X-Correlation-ID"] = cid
        return response

    @app.exception_handler(AegisError)
    async def aegis_error_handler(request: Request, exc: AegisError) -> JSONResponse:
        """Typed errors become predictable responses.

        Clients get a stable ``code`` they can branch on. Internals are never
        echoed - ``context`` is curated by the raiser, not a stack dump.
        """
        cid = getattr(request.state, "correlation_id", "")
        level = log.warning if exc.http_status < 500 else log.error
        level(
            "request failed",
            path=request.url.path,
            code=exc.code,
            status=exc.http_status,
            error=exc.message,
        )
        headers = {"X-Correlation-ID": cid}
        if isinstance(exc, DatabaseResuming):
            headers["Retry-After"] = str(settings.database_resume_retry_after_s)
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.to_dict(), "correlation_id": cid},
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        cid = getattr(request.state, "correlation_id", "")
        log.exception("unhandled error", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=500,
            content={
                "error": {"code": "INTERNAL_ERROR", "message": "internal error"},
                "correlation_id": cid,
            },
        )

    app.include_router(health.router)
    for module in (
        access,
        alerts,
        incidents,
        stream,
        policy_admin,
        actions,
        approvals,
        audit,
        deployments,
        evaluation,
        graph,
        integrations,
        investigations,
        reliability,
        systems,
        tasks,
        war_room,
    ):
        app.include_router(module.router, prefix="/v1")
    # Last: the health router already serves /metrics, and installing after it
    # attaches these series to that endpoint instead of shadowing it.
    install_metrics(app)

    if settings.otel_traces_enabled:
        try:
            from aegis.telemetry.otel import instrument_app

            instrument_app(app, settings)
        except Exception as exc:  # noqa: BLE001 - observability is never fatal
            log.warning("otel instrumentation skipped", error=str(exc))

    return app


app = create_app()


def _database_resuming_response(
    request: Request, exc: DatabaseResuming, settings: Settings
) -> JSONResponse:
    """Stable cold-resume contract; an unavailable database is never empty success."""
    cid = getattr(request.state, "correlation_id", "")
    return JSONResponse(
        status_code=503,
        content={
            "error": exc.to_dict(),
            "correlation_id": cid,
            "retry_after_seconds": settings.database_resume_retry_after_s,
        },
        headers={
            "Retry-After": str(settings.database_resume_retry_after_s),
            "X-Correlation-ID": cid,
        },
    )
