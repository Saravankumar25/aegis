"""FastAPI application factory (ESD §4, §7).

The API owns HTTP concerns, auth, and ingestion; it never runs agent reasoning inline —
ingestion commits an ``open`` incident row and the NOTIFY doubles as the worker wake-up.
Run locally:  uvicorn api.main:app --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from api.errors import AegisError, install_error_handlers
from api.events import hub
from api.routes import auth, incidents, runbooks
from core.config import get_settings
from core.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    await hub.start()
    get_logger(component="api").info("api_started")
    yield
    await hub.stop()


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
    async def webhook_guard(request: Request, call_next):
        """Shared-secret check on the ingestion webhook when a token is configured."""
        settings = get_settings()
        if (
            settings.ingest_webhook_token
            and request.url.path == "/api/v1/incidents"
            and request.method == "POST"
            and request.headers.get("x-aegis-webhook-token") != settings.ingest_webhook_token
        ):
            raise AegisError("webhook_unauthorized", "bad webhook token", status_code=401)
        return await call_next(request)

    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(incidents.router, prefix="/api/v1")
    app.include_router(runbooks.router, prefix="/api/v1")

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
