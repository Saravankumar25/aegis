"""DB-backed integration test fixtures.

Runs against a dedicated ``aegis_test`` database on the local compose Postgres (created on
first use, migrated via Alembic). If Postgres is unreachable the whole directory is skipped —
unit/contract suites stay green without Docker.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]

# Pinned rather than inherited from the developer's `.env`. Leaving it ambient made these
# tests pass only while ingestion was *unauthenticated*: setting a real token locally turned
# every ingestion test into a 401, and the security control itself was never exercised. Now
# the guard is on for every ingestion test, and `test_ingest_rejects_*` proves it rejects.
TEST_WEBHOOK_TOKEN = "test-webhook-token"  # noqa: S105 — fixture value, not a credential
WEBHOOK_HEADERS = {"x-aegis-webhook-token": TEST_WEBHOOK_TOKEN}


def _test_url() -> str:
    return get_settings().database_url.rsplit("/", 1)[0] + "/aegis_test"


async def _ensure_test_db() -> None:
    admin_dsn = (
        get_settings()
        .database_url.replace("postgresql+asyncpg://", "postgresql://")
        .rsplit("/", 1)[0]
        + "/postgres"
    )
    conn = await asyncpg.connect(admin_dsn, timeout=3)
    try:
        exists = await conn.fetchrow("SELECT 1 FROM pg_database WHERE datname = 'aegis_test'")
        if exists is None:
            await conn.execute("CREATE DATABASE aegis_test")
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def db_url() -> str:
    """Create + migrate aegis_test once per session; skip suite if Postgres is down."""
    try:
        asyncio.run(_ensure_test_db())
    except (TimeoutError, OSError, asyncpg.PostgresError):
        pytest.skip("Postgres not reachable — start it with `docker compose up -d`")

    url = _test_url()
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stdout}\n{result.stderr}")
    return url


@pytest.fixture
async def session(db_url: str) -> AsyncIterator[AsyncSession]:
    """Fresh engine + truncated tables per test, so tests never depend on each other."""
    engine = create_async_engine(db_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE incidents, incident_state_transitions, agent_steps, agent_messages, "
                "evidence_citations, refresh_sessions, users, runbooks, audit_log, "
                "remediation_actions, resource_leases, action_circuit_breaker_events, "
                "approvals, memory_summaries, system_flags CASCADE"
            )
        )
    async with maker() as s:
        yield s
        await s.rollback()
    await engine.dispose()


@pytest.fixture
async def api_client(db_url: str, session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """In-process ASGI client against the real app wired to aegis_test.

    ASGITransport does not run the lifespan, so the event hub is not started here —
    ``publish_event`` still works (plain pg_notify); SSE consumption is exercised in the
    end-to-end verification instead. Settings + engine caches are re-pointed at aegis_test.
    """
    os.environ["DATABASE_URL"] = db_url
    os.environ["ENVIRONMENT"] = "test"
    os.environ["INGEST_WEBHOOK_TOKEN"] = TEST_WEBHOOK_TOKEN
    get_settings.cache_clear()
    import core.db as core_db

    core_db._engine = None
    core_db._sessionmaker = None

    from api.deps import get_llm_provider
    from api.main import create_app

    app = create_app()
    # No live model calls from the integration suite. Without this, resolving an incident
    # invoked the Memory agent against the real provider, which made these tests slow,
    # non-deterministic, and dependent on upstream quota to pass. `None` exercises the
    # documented degradation path, which is itself worth covering here.
    app.dependency_overrides[get_llm_provider] = lambda: None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        # Sent by default so ingestion tests exercise the guarded path. A test that needs
        # the unauthenticated case overrides the header explicitly, which makes the
        # negative case visible at the call site instead of implied by configuration.
        headers=WEBHOOK_HEADERS,
    ) as client:
        yield client

    get_settings.cache_clear()
    core_db._engine = None
    core_db._sessionmaker = None
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("ENVIRONMENT", None)
    os.environ.pop("INGEST_WEBHOOK_TOKEN", None)
