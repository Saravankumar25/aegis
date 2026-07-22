# Runbook: cascading failure across dependent services

## Symptoms

Several services alert within a short window, and the alerts follow the call graph rather
than arriving randomly. The service customers notice first is usually the furthest
downstream, not the one that broke. Errors and latency propagate outward from a single
origin, and the affected set matches "everything that calls X" rather than "everything on
node Y" or "everything deployed today".

## Likely causes

One dependency failing and its callers failing with it. Retry amplification, where each layer
retries and multiplies load on an already-struggling service. Timeouts configured longer at
the caller than the callee, so callers hold resources waiting on calls that cannot succeed.
A shared resource — database, cache, message broker — degrading beneath several services at
once. A missing or ineffective circuit breaker allowing failure to propagate unchecked.

## Diagnostic checks

Order the alerts by time and map them onto the service dependency graph; the earliest alert
at the deepest point in the graph is the likely origin. Distinguish shared-infrastructure
failure from propagation: services that do not call each other failing together points at
something they share, not at a cascade. Check retry configuration for amplification. Compare
timeout budgets across layers — a caller timeout longer than its callee's guarantees held
resources during any downstream fault.

## Mitigation

Fix the origin service first. Mitigating downstream symptoms while the origin still fails
consumes effort without recovering the system. Shed load or open circuit breakers at the
boundary to stop amplification while the origin recovers. Only scale intermediate services
once the origin is healthy, since scaling them earlier increases pressure on the failing
dependency.

## Escalation

Escalate to every owning team along the path once the origin is identified, so downstream
teams stop investigating symptoms of someone else's fault.
