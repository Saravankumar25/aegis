# Runbook: connection pool exhaustion

## Symptoms

Requests fail with upstream timeouts while the downstream service itself reports healthy.
Application logs contain `pool exhausted`, `0 idle connections`, `no available connections`,
or `connection pool timeout`. Errors appear at the caller, not the callee. The wait time
before failure clusters tightly around a configured timeout value rather than varying, which
is the signature of queueing for a connection rather than a slow response.

## Likely causes

A pool sized below peak concurrency. A downstream slowdown holding connections open longer,
so the same pool serves fewer requests per second. Connections leaked by a code path that
fails to release them. Retry storms multiplying in-flight requests. A replica count reduced
without resizing pools, concentrating the same traffic on fewer pools.

## Diagnostic checks

Confirm the failure is at the caller by comparing both services' error rates: a healthy
callee with a failing caller points here rather than at the dependency. Compare pool
utilisation against maximum pool size. Measure how long requests wait for a connection versus
how long the downstream call takes once acquired. Check whether the downstream service's
latency rose shortly before the caller's errors, which makes this a symptom rather than the
root cause. Look for recent changes to pool size, timeout, or replica count.

## Mitigation

Scale out the calling service for immediate relief: each replica brings its own pool, so
total capacity rises without a config change. Raise the pool size when concurrency genuinely
exceeds it. If a downstream slowdown is holding connections, fix that first — enlarging the
pool against a slow dependency only moves the queue.

## Escalation

Escalate to the downstream owner when connection hold time rose without any change on the
calling side.
