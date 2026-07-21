"""Redis cache + rate limiter contract (ESD §11, CLAUDE.md §5).

The properties under test are the ones that would be dangerous to get wrong:

* Redis is non-authoritative, so **every** path fails open. A Redis outage must degrade
  throughput, never availability or correctness.
* Write tools are **never** cached. A cached success for a `restart_pod` that never ran would
  be indistinguishable from a real one at the point an operator decides to trust it.
"""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from agents.gateway import _CACHEABLE_READS, _evidence_cache_key
from core import redis as core_redis


class _DeadRedis:
    """Every operation raises, as a downed Redis would."""

    def __getattr__(self, _name):
        def _raise(*_args, **_kwargs):
            raise RedisConnectionError("redis is down")

        return _raise


@pytest.fixture
def dead_redis(monkeypatch):
    monkeypatch.setattr(core_redis, "get_redis", lambda: _DeadRedis())


# --- fail-open contract --------------------------------------------------------------------


async def test_cache_get_returns_none_when_redis_is_down(dead_redis):
    assert await core_redis.cache_get("any-key") is None


async def test_cache_set_swallows_outage(dead_redis):
    await core_redis.cache_set("k", {"v": 1}, ttl_seconds=30)  # must not raise


async def test_rate_limit_allows_when_redis_is_down(dead_redis):
    """Fail open: a cache outage must not become an API outage."""
    decision = await core_redis.check_rate_limit("client", limit=10, window_seconds=60)
    assert decision.allowed is True


async def test_ping_reports_false_rather_than_raising(dead_redis):
    assert await core_redis.ping() is False


async def test_redis_stats_empty_when_down(dead_redis):
    assert await core_redis.redis_stats() == {}


async def test_corrupt_cache_entry_is_treated_as_a_miss(monkeypatch):
    class _Corrupt:
        async def get(self, _key):
            return "{not valid json"

    monkeypatch.setattr(core_redis, "get_redis", lambda: _Corrupt())
    assert await core_redis.cache_get("k") is None


# --- evidence cache keys -------------------------------------------------------------------


@pytest.mark.parametrize(
    "server,tool",
    [
        ("k8s", "restart_pod"),
        ("k8s", "scale_deployment"),
        ("slack", "post_message"),
    ],
)
def test_state_changing_tools_are_never_cacheable(server, tool):
    assert _evidence_cache_key(server, tool, {"pod": "checkout-1"}) is None


def test_unknown_tool_is_not_cacheable_by_default():
    """A tool added later is uncacheable until deliberately allowlisted."""
    assert _evidence_cache_key("k8s", "some_future_tool", {}) is None


def test_cacheable_read_produces_a_key():
    key = _evidence_cache_key("prometheus", "query_metrics", {"query": "up"})
    assert key is not None
    assert key.startswith("mcp:prometheus:query_metrics:")


def test_key_is_independent_of_argument_ordering():
    """Two callers asking the same question must share one entry."""
    a = _evidence_cache_key("k8s", "get_pod_logs", {"pod": "p", "lines": 100})
    b = _evidence_cache_key("k8s", "get_pod_logs", {"lines": 100, "pod": "p"})
    assert a == b


def test_different_arguments_produce_different_keys():
    a = _evidence_cache_key("k8s", "get_pod_logs", {"pod": "checkout-1"})
    b = _evidence_cache_key("k8s", "get_pod_logs", {"pod": "payment-1"})
    assert a != b


def test_allowlist_contains_no_write_verbs():
    """Structural guard: a write tool must never reach the allowlist by review oversight."""
    write_prefixes = ("restart_", "scale_", "post_", "delete_", "create_", "update_", "patch_")
    offenders = [
        (server, tool) for server, tool in _CACHEABLE_READS if tool.startswith(write_prefixes)
    ]
    assert offenders == []
