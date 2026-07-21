# Runbook: elevated 5xx error rate right after a deploy

Symptoms: http_requests_total{status="500"} rate jumps within minutes of a deploy or
config change; errors concentrated in one service while dependencies stay healthy.

Likely causes: regression in the deployed commit, bad config value, incompatible schema
change, feature flag flipped with the deploy.

Checks: compare error-rate step change against deploy timestamps (2h lookback), read the
diff of the most recent commits, check whether dependent services see the errors too
(topology: if only the caller errors, suspect the caller's change).

Fix: roll back the deploy; if rollback is impossible, feature-flag off the change.
