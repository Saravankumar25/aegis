"""One-off schema migration command for deploy jobs and operator runbooks."""

from __future__ import annotations

import asyncio

from aegis.core.config import get_settings
from aegis.core.logging import configure_logging, get_logger
from aegis.persistence.db import Database
from aegis.persistence.migrate import run_migrations

log = get_logger(__name__)


async def migrate() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    db = Database(settings)
    try:
        await db.connect()
        applied = await run_migrations(db)
        log.info("migration command complete", applied=len(applied))
        return 0
    finally:
        await db.close()


def main() -> int:
    return asyncio.run(migrate())


if __name__ == "__main__":
    raise SystemExit(main())
