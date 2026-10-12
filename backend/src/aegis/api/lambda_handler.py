"""AWS Lambda adapter for the unchanged FastAPI router/auth contract."""

from __future__ import annotations

from mangum import Mangum

from aegis.api.app import app

# Lifespan stays enabled: one warm Lambda execution environment owns one pool
# and one verifier, exactly as one service process does. Migrations are disabled
# by API_RUN_MIGRATIONS=false in the Lambda environment and run separately.
handler = Mangum(app, lifespan="auto")

__all__ = ["handler"]
