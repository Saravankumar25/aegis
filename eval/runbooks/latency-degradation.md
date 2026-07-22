# Runbook: p99 latency degradation

## Symptoms

Requests are taking several seconds but nothing is erroring. `http_request_duration_seconds`
p99 rises without a matching error-rate spike. Callers log timeouts while the service itself
reports success. Queue depth grows and connection pool utilisation approaches its limit.
Throughput stays flat or falls while latency climbs, which distinguishes saturation from a
traffic increase.

## Likely causes

A downstream dependency slowing down, propagating latency to everything that calls it. CPU
throttling when the container hits its quota. An N+1 query pattern or a cache-miss storm
after a cache configuration change. Connection pool saturation, where requests spend their
time waiting for a connection rather than doing work. Lock contention under higher
concurrency.

## Diagnostic checks

Break latency down by dependency to find which call is slow — if one dependency accounts for
the increase, investigate that service rather than this one. Check CPU throttling metrics
against the container quota. Read recent commits touching timeouts, pool sizes, cache
configuration, or query patterns. Compare pool wait time against total request duration: time
spent waiting for a connection is a sizing problem, not a slow dependency.

## Mitigation

Scale out replicas for immediate relief when the service is saturated — more replicas means
more total pool capacity and less queueing per instance. Revert the change that shifted cache
or pool behaviour. Raise timeouts only with evidence that the downstream call is legitimately
slower, because a raised timeout without that evidence converts a fast failure into a slow
one and consumes more connections while it waits.

## Escalation

Escalate to the owning team of the slow dependency once latency is attributed to it; scaling
this service will not fix a bottleneck that lives elsewhere.
