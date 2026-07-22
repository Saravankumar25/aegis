# Runbook: service partially or fully unavailable

## Symptoms

Customers cannot reach the service at all, or only some requests succeed. Readiness probes
are failing, the deployment's ready-replica count sits below desired, and the `app_up` gauge
reads 0 for one or more pods. Kubernetes emits Warning events such as BackOff,
FailedScheduling, or Unhealthy. Load balancers remove endpoints as probes fail, so the
service disappears from routing rather than returning errors.

## Likely causes

A crash loop preventing pods from reaching Ready. An image pull failure after a tag change
or registry credential expiry. Node pressure evicting pods or blocking scheduling. A probe
misconfigured by a recent manifest change, where the application is healthy but the probe
path, port, or timeout is wrong. Insufficient cluster capacity for the requested resources.

## Diagnostic checks

Read Kubernetes events for the namespace, newest first. Compare deployment ready versus
desired replica counts. Inspect pod container states and last termination reasons — a
non-zero exit code points at the application, while a probe timeout points at the probe.
Check recent manifest commits for changes to probe definitions, resource requests, or image
tags. Confirm whether nodes are under memory or disk pressure.

## Mitigation

Restart or reschedule the affected pods for immediate relief. Fix the probe definition or
image reference if a manifest change introduced the fault. Scale up the node pool when node
pressure is the cause. If the crash loop is the cause, follow the OOMKilled runbook first —
availability will not recover until pods stop dying.

## Escalation

Escalate when pods cannot be scheduled anywhere, which indicates a cluster capacity problem
outside the service team's control.
