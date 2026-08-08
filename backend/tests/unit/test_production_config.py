"""The production start-up guard (CLAUDE.md §12).

Aegis's permissive defaults are correct for the documented local target — a trusted kind
cluster on a laptop (ESD §18) — and dangerous the moment the same process listens on a public
address. The failure mode that matters is not that the defaults exist; it is that they are
*silent*. A running instance with no ingestion token looks exactly like an authenticated one
until someone else's alert has already been accepted, spent LLM budget, and raised a
remediation proposal.

These tests assert the guard fires on each unsafe value independently, and — just as
importantly — that it stays out of the way everywhere else. A guard that also trips locally
gets disabled, and a disabled guard protects nothing.
"""

from __future__ import annotations

import pytest

from core.config import INSECURE_JWT_DEFAULT, Settings

# A configuration that must be accepted, used as the base each case degrades one field of.
# Written out rather than built by helper so a reader can see exactly what "safe" means here.
SAFE = {
    "environment": "production",
    "jwt_secret": "a" * 64,
    "ingest_webhook_token": "a-real-shared-secret",
    "cors_origins": "https://aegis.example.com",
    "database_url": "postgresql+asyncpg://aegis:realpassword@10.0.0.5:5432/aegis",
}


def test_fully_configured_production_starts() -> None:
    """The guard must not block a correct deployment — otherwise it gets removed."""
    settings = Settings(**SAFE)
    assert settings.environment == "production"


@pytest.mark.parametrize("marker", ["production", "prod", "PRODUCTION", "  Prod  "])
def test_production_is_recognised_however_it_is_spelled(marker: str) -> None:
    """Case and whitespace must not be a way to slip past the guard.

    `ENVIRONMENT=Production` set by hand in a systemd unit or a Docker env file is the
    likeliest spelling after `production`, and matching it exactly would silently disarm
    every check below while looking correctly configured.
    """
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(**{**SAFE, "environment": marker, "jwt_secret": INSECURE_JWT_DEFAULT})


@pytest.mark.parametrize("marker", ["local", "test", "staging", "dev"])
def test_non_production_environments_are_untouched(marker: str) -> None:
    """Every default stays permissive off production — including staging.

    Staging is deliberately NOT covered: it is where a deployment is rehearsed, and forcing
    the full production credential set there is the friction that leads to reusing production
    secrets in a less protected place.
    """
    settings = Settings(environment=marker, jwt_secret=INSECURE_JWT_DEFAULT)
    assert settings.jwt_secret == INSECURE_JWT_DEFAULT


def test_placeholder_jwt_secret_is_refused() -> None:
    """The published placeholder means anyone with the repo can mint a session cookie."""
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(**{**SAFE, "jwt_secret": INSECURE_JWT_DEFAULT})


def test_short_jwt_secret_is_refused() -> None:
    """Not the placeholder, but still brute-forceable — a distinct failure, distinctly named."""
    with pytest.raises(ValueError, match="32"):
        Settings(**{**SAFE, "jwt_secret": "short"})


def test_missing_ingest_webhook_token_is_refused() -> None:
    """`webhook_guard` skips the check entirely when no token is set, so an unset token is
    not a weaker guard — it is no guard, on the one unauthenticated write path Aegis has."""
    with pytest.raises(ValueError, match="INGEST_WEBHOOK_TOKEN"):
        Settings(**{**SAFE, "ingest_webhook_token": None})


def test_empty_ingest_webhook_token_is_refused() -> None:
    """An empty string is how this arrives from a Docker env file, and it is equally unset."""
    with pytest.raises(ValueError, match="INGEST_WEBHOOK_TOKEN"):
        Settings(**{**SAFE, "ingest_webhook_token": ""})


@pytest.mark.parametrize(
    "origins",
    ["*", "https://aegis.example.com,*", " * ", "*,https://aegis.example.com"],
)
def test_wildcard_cors_is_refused_in_any_position(origins: str) -> None:
    """`allow_credentials=True` is set in the app factory, so a wildcard origin would let any
    site read authenticated responses. Checked per entry, because the wildcard is most likely
    to be appended to an existing list rather than to stand alone."""
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(**{**SAFE, "cors_origins": origins})


def test_example_database_password_is_refused() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings(
            **{**SAFE, "database_url": "postgresql+asyncpg://aegis:change-me-locally@db:5432/aegis"}
        )


def test_every_problem_is_reported_at_once() -> None:
    """One-at-a-time failures mean one redeploy per mistake. The operator should see the whole
    list on the first attempt."""
    with pytest.raises(ValueError) as exc:
        Settings(
            environment="production",
            jwt_secret=INSECURE_JWT_DEFAULT,
            ingest_webhook_token=None,
            cors_origins="*",
            database_url="postgresql+asyncpg://aegis:change-me-locally@db:5432/aegis",
        )
    message = str(exc.value)
    for expected in ("JWT_SECRET", "INGEST_WEBHOOK_TOKEN", "CORS_ORIGINS", "DATABASE_URL"):
        assert expected in message
