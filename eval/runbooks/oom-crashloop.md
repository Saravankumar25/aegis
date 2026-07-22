# Runbook: OOMKilled / CrashLoopBackOff pods

## Symptoms

Containers keep dying and restarting. Pod restart counts climb steadily, container status
shows `OOMKilled`, and the pod enters `CrashLoopBackOff` as Kubernetes backs off between
restart attempts. Readiness flaps as pods briefly become ready and then die again. Memory
usage rises between restarts rather than plateauing. The kernel out-of-memory killer
terminates the process, so there is usually no application stack trace — it simply stops
mid-request.

## Likely causes

A memory limit set below the application's genuine working set. A memory leak introduced by
a recent change, growing with uptime or request volume. An unbounded in-memory cache, often
after a config change to cache TTLs or sizes. A large request or response body buffered
entirely in memory. An increased batch size raising peak allocation.

## Diagnostic checks

Describe the pod and read the last termination reason; exit code 137 means the kernel killed
it. Compare `container_memory_working_set_bytes` against the configured limit in the window
before the kill. Determine whether memory grows monotonically with uptime, which indicates a
leak, or spikes with traffic, which indicates a sizing problem. Check recent commits touching
caching, batch sizes, or buffering. Compare restart counts across replicas: all replicas
dying points at sizing or a leak, one replica dying points at a poison request.

## Mitigation

Revert the offending change when the memory growth began with a deploy. Raise the container
memory limit deliberately when the working set is genuinely larger than the limit — as a
sizing decision, not a band-aid. Restart the affected pods for immediate relief while the
real fix lands.

## Escalation

Escalate to the owning team when memory grows monotonically regardless of the limit, which is
a leak requiring a code fix rather than an infrastructure change.
