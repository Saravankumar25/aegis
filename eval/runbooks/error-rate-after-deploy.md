# Runbook: elevated 5xx error rate right after a deploy

## Symptoms

Error responses climb within minutes of shipping a release, a rollout, or a config change.
`http_requests_total{status="500"}` shows a step change rather than a gradual ramp, and the
step lines up with a deployment timestamp. Errors stay concentrated in the service that was
deployed while its dependencies keep serving normally. Customers report that something
broke right after a release went out, and the service was healthy immediately before it.

## Likely causes

A regression in the deployed commit. A bad configuration value shipped alongside the code.
An incompatible database schema change applied out of order with the rollout. A feature flag
flipped as part of the release. A dependency version bump that changed behaviour silently.

## Diagnostic checks

Compare the error-rate step change against deploy timestamps over a two-hour lookback; the
correlation is the whole signal here, and without a deploy inside the window this runbook
does not apply. Read the diff of the most recent commits to the affected service. Check
whether dependent services see the same errors: if only the caller is erroring while its
dependencies are healthy, suspect the caller's own change rather than an upstream fault.
Confirm the previous release was healthy at the same traffic level.

## Mitigation

Roll back the deploy to the last known-good revision. This is the fastest reliable relief
and does not require understanding the regression first. If rollback is not possible because
of a forward-only schema migration, disable the change behind its feature flag instead.

## Escalation

Escalate to the owning team if rollback does not restore the error rate within ten minutes,
which usually means the deploy was correlated with the incident rather than its cause.
