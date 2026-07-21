# Runbook: service partially or fully unavailable

Symptoms: readiness probe failures, deployment ready-replica count below desired, k8s
Warning events (BackOff, FailedScheduling, Unhealthy), app_up gauge at 0.

Likely causes: crash loop (see OOM runbook), image pull failure, node pressure,
misconfigured probe after a manifest change.

Checks: kubectl events for the namespace, deployment ready vs desired counts, pod
container states and last termination reasons, recent manifest commits.

Fix: restart or reschedule the pod for immediate relief; fix the probe or image reference
if the manifest changed; scale up if node pressure is the cause.
