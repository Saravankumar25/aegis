# Runbook: OOMKilled / CrashLoopBackOff pods

Symptoms: pod restarts climbing, container state waiting with reason CrashLoopBackOff,
lastState terminated with reason OOMKilled, readiness flapping.

Likely causes: memory limit too low for workload, memory leak after a recent change,
cache growth unbounded (check recent config changes to cache TTLs or sizes).

Checks: kubectl describe pod (termination reason), memory usage vs limit in Prometheus
(container_memory_working_set_bytes), recent commits touching caching or batch sizes.

Fix: revert the offending change, or raise the memory limit deliberately (not as a
band-aid), or restart the pod for immediate relief while the fix lands.
