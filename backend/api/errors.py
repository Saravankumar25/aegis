"""Structured error envelope for every 4xx/5xx response (ESD §12).

No bare stack trace ever reaches a client: handlers convert exceptions into
``{error_code, message, incident_id}`` and unexpected exceptions are logged server-side
with full context before returning an opaque 500.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.logging import get_logger


class AegisError(Exception):
    """Domain error with a stable machine-readable code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status_code: int = 400,
        incident_id: uuid.UUID | str | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.incident_id = str(incident_id) if incident_id else None


def _envelope(error_code: str, message: str, incident_id: str | None = None) -> dict:
    return {"error_code": error_code, "message": message, "incident_id": incident_id}


def install_error_handlers(app: FastAPI) -> None:
    """Register the envelope handlers on the app."""

    @app.exception_handler(AegisError)
    async def aegis_error(request: Request, exc: AegisError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(exc.error_code, exc.message, exc.incident_id),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found"}.get(
            exc.status_code, "http_error"
        )
        return JSONResponse(status_code=exc.status_code, content=_envelope(code, str(exc.detail)))

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_envelope("validation_error", "request failed schema validation"),
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        get_logger(component="api").exception("unhandled_api_exception", path=request.url.path)
        return JSONResponse(
            status_code=500, content=_envelope("internal_error", "internal server error")
        )
