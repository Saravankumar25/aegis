# Runbook: database slow or unavailable

## Symptoms

Every service that queries the database degrades together, which distinguishes a database
fault from a single-service bug. Query latency rises, connection attempts fail or time out,
and application logs show `too many connections`, `deadlock detected`, `statement timeout`,
or `could not connect`. Read-heavy endpoints degrade before write-heavy ones when replicas
are lagging. Error rates rise across unrelated services simultaneously.

## Likely causes

Connection limit reached, often because application pools multiplied across replicas exceed
the server's maximum. A long-running query or migration holding locks. A missing index after
a schema change, turning an indexed lookup into a sequential scan. Replica lag serving stale
or failing reads. Disk pressure or exhausted IOPS on the database host. Vacuum or maintenance
work saturating the instance.

## Diagnostic checks

Confirm the blast radius: if several unrelated services degraded at the same moment, suspect
shared infrastructure rather than any one of them. Compare active connections against the
server maximum. Look for long-running queries and blocking locks. Check replica lag. Correlate
the onset against recent migrations, index changes, or traffic growth. Check host-level disk
and IOPS saturation.

## Mitigation

Terminate the blocking query or migration when one is identified. Reduce application pool
sizes or replica counts to bring total connections under the server limit — counterintuitive
during an incident, but a database refusing every connection serves nobody. Fail reads over
to a healthy replica. Add the missing index once the slow query is identified.

## Escalation

Escalate to the database owner immediately when the instance is refusing connections
outright; application-side mitigation cannot recover a saturated database.
