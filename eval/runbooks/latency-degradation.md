# Runbook: p99 latency degradation

Symptoms: http_request_duration_seconds p99 rising without a matching error spike;
timeouts in caller logs; queue depth or connection pool saturation.

Likely causes: dependency slowdown (topology: check the services this one calls),
resource saturation (CPU throttling), N+1 or cache-miss storm after a cache change.

Checks: latency by dependency, CPU throttling metrics, recent commits touching timeouts,
pool sizes, or cache configuration.

Fix: scale out replicas for immediate relief; revert the change that shifted cache or
pool behavior; raise timeouts only with evidence.
