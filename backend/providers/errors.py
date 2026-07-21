"""Provider-independent failure types (ESD §20).

These live outside any single provider module because callers must be able to catch
them without knowing which provider is configured. The worker's degradation path —
leave the incident in `investigating`, let the reconciliation sweep retry — is
identical whether the throttling came from OpenRouter or Gemini, so the exception
type has to be identical too. Previously these were defined inside
``providers.openrouter``, which made "handle exhaustion" implicitly mean "import the
OpenRouter module", and that is the wrong dependency direction for a Strategy.

The distinction between `RateLimited`, `ProviderExhausted` and `DailyQuotaExhausted`
is deliberate and operator-facing: they differ in what a human should *do*, not just
in what went wrong.
"""

from __future__ import annotations


class RateLimited(Exception):
    """Upstream said 429 (or rejected the key) — try another key/model.

    Recoverable within a single call by rotating; never surfaces to the caller.
    """


class ProviderExhausted(RuntimeError):
    """Every configured model and key was unavailable.

    Raised rather than falling back to canned text: no answer is better than a fake
    one, because a fabricated analysis is indistinguishable from a real one at
    exactly the moment a human decides to trust it (CLAUDE.md §18).
    """


class DailyQuotaExhausted(ProviderExhausted):
    """The account's daily allowance is spent — distinct from momentary throttling.

    Worth its own type because the operator response is completely different: a
    per-minute throttle clears itself in seconds and the retry sweep handles it,
    whereas a daily cap needs either a wait for the reset or credits on the account.
    Collapsing both into "rate limited" sends people to re-run a command that cannot
    possibly succeed for hours.
    """


class StructuredOutputError(RuntimeError):
    """The model could not be coerced into the required schema within the repair budget.

    Raised rather than returning a partially-valid object: a caller that branches on
    this output cannot tell a hallucinated field from a real one, so a half-parsed
    result is more dangerous than a visible failure.
    """
