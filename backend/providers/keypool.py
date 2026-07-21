"""Credential rotation state for multi-key LLM providers (ESD §20).

Extracted from the provider because "which key should I try next" is real logic with
real failure modes, not a loop counter, and it is far easier to test in isolation than
through a mocked HTTP transport.

The central distinction is between the two ways a key becomes unusable:

- **Throttled** (per-minute rate limit): transient, clears itself in seconds. The key
  is *deprioritised* for a short cooldown but never removed — if every key is cooling
  down, the pool still hands them back rather than reporting failure, because a
  cooldown is an optimisation and refusing to try would turn a brief throttle into a
  hard outage.
- **Quota-exhausted** (per-day cap): does not clear until the quota window resets. The
  key is genuinely skipped, and when *every* key is in this state the caller is told
  so, because no amount of retrying can succeed.

That second distinction is provider-specific and was previously wrong for Gemini. The
OpenRouter provider treats a daily cap as terminal for the whole call, which is correct
there: its keys bill against one account, so one key's daily cap is every key's daily
cap. Gemini keys carry independent per-project quotas, so treating the first exhausted
key as terminal would strand four working keys and report the system as out of capacity
while 80% of it was idle.

Quota state is in-process only. It is an optimisation to avoid re-probing a key known
to be capped; a restart simply re-learns it on the next 429, which costs one request.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

# A per-minute throttle clears fast. Long enough to stop hammering one key inside a
# single investigation, short enough that a key is not sidelined for a whole incident.
THROTTLE_COOLDOWN_SECONDS = 30.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _next_quota_reset(now: datetime) -> datetime:
    """Google's daily quotas reset at midnight Pacific, but the exact boundary is not
    documented as a stable contract and varies by quota. Next UTC midnight is used as a
    conservative approximation: being wrong here only costs one probe request, because
    a still-capped key simply re-marks itself.
    """
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


class KeyPool:
    """Ordered credential selection with per-key throttle and quota state."""

    def __init__(self, keys: list[str], *, clock: Callable[[], datetime] = _utcnow) -> None:
        if not keys:
            raise ValueError("KeyPool requires at least one key")
        self._keys = list(keys)
        self._clock = clock
        self._cursor = 0
        self._quota_until: dict[int, datetime] = {}
        self._throttled_until: dict[int, datetime] = {}

    def __len__(self) -> int:
        return len(self._keys)

    def key(self, index: int) -> str:
        return self._keys[index % len(self._keys)]

    # --- state transitions ----------------------------------------------------------

    def mark_success(self, index: int) -> None:
        """Make this key the starting point for the next call and clear its penalties."""
        self._cursor = index % len(self._keys)
        self._throttled_until.pop(self._cursor, None)
        self._quota_until.pop(self._cursor, None)

    def mark_throttled(self, index: int) -> None:
        i = index % len(self._keys)
        self._throttled_until[i] = self._clock() + timedelta(seconds=THROTTLE_COOLDOWN_SECONDS)

    def mark_quota_exhausted(self, index: int) -> None:
        i = index % len(self._keys)
        self._quota_until[i] = _next_quota_reset(self._clock())

    # --- selection ------------------------------------------------------------------

    def _quota_blocked(self, index: int) -> bool:
        until = self._quota_until.get(index)
        if until is None:
            return False
        if self._clock() >= until:
            del self._quota_until[index]
            return False
        return True

    def _throttled(self, index: int) -> bool:
        until = self._throttled_until.get(index)
        if until is None:
            return False
        if self._clock() >= until:
            del self._throttled_until[index]
            return False
        return True

    def all_quota_exhausted(self) -> bool:
        """True when every key has hit its daily cap — the only unrecoverable state."""
        return all(self._quota_blocked(i) for i in range(len(self._keys)))

    def order(self) -> list[int]:
        """Indices to try, best first.

        Round-robin from the last successful key so a throttled key is not retried first
        every single time, with keys that are merely cooling down demoted to the back
        rather than dropped. Quota-exhausted keys are omitted entirely — unless that
        would empty the pool, in which case the caller detects `all_quota_exhausted()`
        and reports the unrecoverable state instead of looping.
        """
        rotated = [(self._cursor + offset) % len(self._keys) for offset in range(len(self._keys))]
        usable = [i for i in rotated if not self._quota_blocked(i)]
        ready = [i for i in usable if not self._throttled(i)]
        cooling = [i for i in usable if self._throttled(i)]
        # Cooling keys are appended, not discarded: exhausting every ready key and then
        # giving up while a key is 5 seconds from being usable is a worse outcome than
        # one extra attempt.
        return ready + cooling

    def snapshot(self) -> dict[str, int]:
        """Counts for logging. Never includes key material (CLAUDE.md §12)."""
        return {
            "keys": len(self._keys),
            "quota_exhausted": sum(1 for i in range(len(self._keys)) if self._quota_blocked(i)),
            "throttled": sum(1 for i in range(len(self._keys)) if self._throttled(i)),
        }
