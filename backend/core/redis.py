"""Redis connection, cache, and rate limiting (ESD §11).

Redis is a **cache and coordination layer, never a source of truth** (CLAUDE.md §5). Postgres
holds every fact the system would be wrong to lose: incidents, remediation actions, resource
leases, circuit-breaker state, the kill switch. Nothing in this module may be promoted to
authoritative, and in particular the V1.5 safety mechanisms deliberately do **not** live here —
a resource lease enforced by a cache that can be flushed is not a safety mechanism.

That principle dictates the degradation policy: **every operation here fails open.** If Redis
is unreachable, a cache read misses and the caller does the real work; a rate-limit check
allows the request. The alternative — failing closed — would convert the loss of a cache into
a total outage, which is a strictly worse failure mode for something explicitly designated
non-authoritative. Load-bearing limits (`tier1_rate_limit_per_hour`, the circuit breaker) are
enforced in Postgres precisely so that this fail-open choice is safe.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from core.config import get_settings
from core.logging import get_logger

_log = get_logger(component="redis")
_client: aioredis.Redis | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def get_redis() -> aioredis.Redis:
    """Redis client over a connection pool, created on first use.

    The client is cached per event loop, not merely per process. ``redis.asyncio`` binds its
    connections to the loop that created them, so a client carried across loops raises
    "Event loop is closed" on the first call against the new one. A long-lived server has a
    single loop and hits the cached instance every time; anything that runs multiple loops in
    one process (``asyncio.run`` more than once, or a test suite with a loop per test) gets a
    fresh client instead of a corrupted one.

    Constructing the client performs no I/O, so this never blocks at import time and a Redis
    outage surfaces at the call site — where it is handled — rather than at startup.
    """
    global _client, _client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if _client is not None and _client_loop is loop:
        return _client

    if _client is not None:
        # Abandon the stale client rather than awaiting aclose(): its loop is already gone,
        # so closing it is impossible and its sockets are dead regardless.
        _log.debug("redis_client_rebound", reason="event_loop_changed")

    settings = get_settings()
    _client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        max_connections=settings.redis_max_connections,
        socket_timeout=settings.redis_timeout_seconds,
        socket_connect_timeout=settings.redis_timeout_seconds,
        health_check_interval=30,
    )
    _client_loop = loop
    return _client


async def close_redis() -> None:
    """Release the pool (application shutdown)."""
    global _client, _client_loop
    if _client is not None:
        try:
            await _client.aclose()
        except (RedisError, OSError, RuntimeError) as exc:
            # Shutdown must not fail because a cache connection could not be closed tidily.
            _log.warning("redis_close_failed", error=str(exc))
        _client = None
        _client_loop = None


async def ping() -> bool:
    """True if Redis answered. Never raises — used by the health endpoint."""
    try:
        return bool(await get_redis().ping())
    except (RedisError, OSError) as exc:
        _log.warning("redis_unreachable", error=str(exc))
        return False


async def redis_stats() -> dict[str, Any]:
    """Operational counters for the metrics endpoint. Empty dict if Redis is down."""
    try:
        info = await get_redis().info()
    except (RedisError, OSError):
        return {}
    hits = info.get("keyspace_hits", 0)
    misses = info.get("keyspace_misses", 0)
    total = hits + misses
    return {
        "connected_clients": info.get("connected_clients"),
        "used_memory_bytes": info.get("used_memory"),
        "keyspace_hits": hits,
        "keyspace_misses": misses,
        # Reported as None rather than 0.0 on an idle instance: a 0% hit rate and "no
        # lookups yet" mean very different things to whoever reads this.
        "hit_rate": round(hits / total, 4) if total else None,
        "uptime_seconds": info.get("uptime_in_seconds"),
    }


# --- cache ---------------------------------------------------------------------------------


async def cache_get(key: str) -> Any | None:
    """Read a JSON value. Returns None on miss, on malformed data, or if Redis is down."""
    try:
        raw = await get_redis().get(key)
    except (RedisError, OSError) as exc:
        _log.warning("cache_unavailable", operation="get", error=str(exc))
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # A corrupt entry must never crash a request path. Treat it as a miss and let it
        # be overwritten by the next write.
        _log.warning("cache_corrupt_entry", key=key)
        return None


async def cache_set(key: str, value: Any, *, ttl_seconds: int) -> None:
    """Write a JSON value with a TTL. A failure is logged and swallowed — it is only a cache.

    Every entry carries a TTL by construction: an unbounded cache of incident evidence would
    grow without limit and, worse, could serve stale infrastructure state during a later
    incident.
    """
    try:
        await get_redis().set(key, json.dumps(value, default=str), ex=ttl_seconds)
    except (RedisError, OSError) as exc:
        _log.warning("cache_unavailable", operation="set", error=str(exc))
    except (TypeError, ValueError) as exc:
        # Non-serialisable payload is a programming error in the caller, not a Redis fault.
        _log.error("cache_unserialisable", key=key, error=str(exc))


async def cache_invalidate(*keys: str) -> None:
    """Drop cache entries. Best effort."""
    if not keys:
        return
    try:
        await get_redis().delete(*keys)
    except (RedisError, OSError) as exc:
        _log.warning("cache_unavailable", operation="delete", error=str(exc))


# --- rate limiting -------------------------------------------------------------------------


class RateLimitDecision:
    """Outcome of a rate-limit check."""

    __slots__ = ("allowed", "remaining", "retry_after_seconds")

    def __init__(self, *, allowed: bool, remaining: int, retry_after_seconds: int) -> None:
        self.allowed = allowed
        self.remaining = remaining
        self.retry_after_seconds = retry_after_seconds


async def check_rate_limit(bucket: str, *, limit: int, window_seconds: int) -> RateLimitDecision:
    """Fixed-window counter for ``bucket``.

    INCR and EXPIRE are pipelined into one round trip and the EXPIRE is set on every call
    rather than only when the counter is created: a key that somehow lost its TTL would
    otherwise block that bucket permanently, which is exactly the sort of latent trap a
    limiter must not have.

    Fails **open** if Redis is unreachable — see the module docstring.
    """
    key = f"ratelimit:{bucket}"
    try:
        pipe = get_redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds)
        count, _ = await pipe.execute()
    except (RedisError, OSError) as exc:
        _log.warning("rate_limit_unavailable", bucket=bucket, error=str(exc))
        return RateLimitDecision(allowed=True, remaining=limit, retry_after_seconds=0)

    allowed = count <= limit
    return RateLimitDecision(
        allowed=allowed,
        remaining=max(0, limit - count),
        retry_after_seconds=0 if allowed else window_seconds,
    )
