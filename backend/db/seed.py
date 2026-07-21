"""Seed local users (one per role) for development and the demo environment.

Passwords come from env vars (never hardcoded — CLAUDE.md §12); defaults exist only for the
local environment and the script refuses to run with defaults when ENVIRONMENT != local/test.
Run:  python -m db.seed
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import select

from api.security import hash_password
from core.config import get_settings
from core.db import session_scope
from core.logging import configure_logging, get_logger
from db.enums import UserRole
from db.models import User

_DEFAULTS = {
    "admin@aegis.dev": (UserRole.admin, "AEGIS_ADMIN_PASSWORD"),
    "oncall@aegis.dev": (UserRole.on_call_engineer, "AEGIS_ONCALL_PASSWORD"),
    "viewer@aegis.dev": (UserRole.viewer, "AEGIS_VIEWER_PASSWORD"),
}
_LOCAL_FALLBACK = "aegis-local-dev"


async def seed_users() -> None:
    settings = get_settings()
    log = get_logger(component="seed")
    async with session_scope() as session:
        for email, (role, env_var) in _DEFAULTS.items():
            password = os.environ.get(env_var)
            if password is None:
                if settings.environment not in ("local", "test"):
                    raise RuntimeError(f"{env_var} must be set outside local/test environments")
                password = _LOCAL_FALLBACK
            existing = (
                await session.execute(select(User).where(User.email == email))
            ).scalar_one_or_none()
            if existing is None:
                session.add(User(email=email, hashed_password=hash_password(password), role=role))
                log.info("user_seeded", email=email, role=role)
            else:
                log.info("user_exists", email=email)


if __name__ == "__main__":
    configure_logging()
    asyncio.run(seed_users())
