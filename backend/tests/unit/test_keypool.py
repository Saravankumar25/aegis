"""Unit tests: credential rotation state (ESD §20).

The pool's job is to answer "which key next" correctly under partial failure. The
distinction that matters — and that was previously wrong for Gemini — is between a
*throttled* key (transient, deprioritise) and a *quota-exhausted* key (skip, and if all
are exhausted, say so). Getting it backwards either strands working keys or hammers
dead ones.

A frozen, manually advanced clock is used throughout so cooldown expiry is asserted
directly rather than slept for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from providers.keypool import THROTTLE_COOLDOWN_SECONDS, KeyPool


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def _pool(n: int, clock: Clock) -> KeyPool:
    return KeyPool([f"key-{i}" for i in range(n)], clock=clock)


# --- ordering -----------------------------------------------------------------------------


def test_starts_in_declaration_order(clock):
    assert _pool(3, clock).order() == [0, 1, 2]


def test_success_makes_that_key_preferred_next_time(clock):
    pool = _pool(3, clock)
    pool.mark_success(2)
    assert pool.order() == [2, 0, 1], "the last working key should be tried first"


def test_throttled_key_is_demoted_not_dropped(clock):
    """A cooldown is an optimisation; refusing to try would turn a blip into an outage."""
    pool = _pool(3, clock)
    pool.mark_throttled(0)
    order = pool.order()
    assert set(order) == {0, 1, 2}, "no key may disappear because of a transient throttle"
    assert order[-1] == 0


def test_throttle_expires_after_the_cooldown(clock):
    pool = _pool(2, clock)
    pool.mark_throttled(0)
    assert pool.order()[0] == 1
    clock.advance(THROTTLE_COOLDOWN_SECONDS + 1)
    assert pool.order() == [0, 1], "an expired cooldown must restore normal ordering"


def test_all_throttled_still_returns_every_key(clock):
    pool = _pool(3, clock)
    for i in range(3):
        pool.mark_throttled(i)
    assert set(pool.order()) == {0, 1, 2}


# --- quota: the semantics that were previously wrong --------------------------------------


def test_quota_exhausted_key_is_skipped_entirely(clock):
    pool = _pool(3, clock)
    pool.mark_quota_exhausted(1)
    assert pool.order() == [0, 2]


def test_one_exhausted_key_does_not_exhaust_the_pool(clock):
    """The Gemini bug: a per-project cap on one key stranded every other key."""
    pool = _pool(5, clock)
    pool.mark_quota_exhausted(0)
    assert pool.all_quota_exhausted() is False
    assert len(pool.order()) == 4


def test_pool_is_exhausted_only_when_every_key_is_capped(clock):
    pool = _pool(3, clock)
    for i in range(2):
        pool.mark_quota_exhausted(i)
        assert pool.all_quota_exhausted() is False
    pool.mark_quota_exhausted(2)
    assert pool.all_quota_exhausted() is True
    assert pool.order() == []


def test_quota_survives_a_throttle_cooldown_window(clock):
    """A daily cap must not evaporate on the 30s throttle timer."""
    pool = _pool(2, clock)
    pool.mark_quota_exhausted(0)
    clock.advance(THROTTLE_COOLDOWN_SECONDS * 10)
    assert pool.order() == [1]


def test_quota_clears_after_the_reset_boundary(clock):
    pool = _pool(2, clock)
    pool.mark_quota_exhausted(0)
    pool.mark_quota_exhausted(1)
    assert pool.all_quota_exhausted() is True
    clock.advance(timedelta(days=1).total_seconds() + 3600)
    assert pool.all_quota_exhausted() is False
    assert set(pool.order()) == {0, 1}


def test_success_clears_a_stale_quota_mark(clock):
    """If a key answers, whatever we believed about its quota was wrong."""
    pool = _pool(2, clock)
    pool.mark_quota_exhausted(0)
    pool.mark_success(0)
    assert pool.order()[0] == 0


# --- hygiene ------------------------------------------------------------------------------


def test_snapshot_never_contains_key_material(clock):
    pool = KeyPool(["super-secret-key"], clock=clock)
    pool.mark_throttled(0)
    assert "super-secret-key" not in str(pool.snapshot())
    assert pool.snapshot()["keys"] == 1


def test_empty_pool_is_rejected_at_construction(clock):
    with pytest.raises(ValueError, match="at least one key"):
        KeyPool([], clock=clock)


def test_index_wraps_so_callers_cannot_overrun(clock):
    pool = _pool(3, clock)
    assert pool.key(4) == pool.key(1)
