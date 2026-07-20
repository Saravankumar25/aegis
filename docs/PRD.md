# PRD.md — Aegis Product Requirements Document

**Status:** Living document. MVP scope locked, V1.5 scope locked, V2 partially scoped.
**Owner:** Solo builder (portfolio/production-grade project)
**Last updated:** 2026-07-20
**Build order:** MVP ships and is demoable on its own before any V1.5 work starts. V1.5 is additive, not a rewrite.

> **Document purpose:** Aegis is built as a demonstration-grade production system. A fictional but realistic mid-size e-commerce company, **Meridian Commerce**, stands in as the target environment. Every requirement below is written as if for a real engineering org running real production traffic, because that's the bar the system is held to.

---

## 1. Problem Statement

Production incidents are expensive and slow to resolve. Industry data puts the cost of downtime at $5,000 to $500,000 per minute depending on company size and the service affected. Despite that, the standard incident response workflow at most companies, including Meridian Commerce, is still largely manual:

1. An alert fires.
2. An on-call engineer is paged, often outside working hours.
3. The engineer opens several disconnected tools (logs, metrics, traces, deploy history, Slack) to piece together what happened.
4. The engineer searches for a relevant runbook, if one exists and if they remember it exists.
5. The engineer forms a hypothesis, tests it, and applies a fix.
6. Someone writes a postmortem afterward, if there's time, which there often isn't.

This process takes 30 to 90 minutes on average even at well-staffed organizations. Knowledge about how past incidents were resolved lives in engineers' heads, in Slack threads that get buried, and in Confluence pages that go stale.

Aegis addresses this in two stages. **MVP** automates the investigative part: gathering evidence and forming a grounded root-cause hypothesis, so a human never starts from a blank dashboard. **V1.5** closes the loop by adding safe, tiered autonomous remediation on top of that investigation, with humans kept explicitly in control of anything above the lowest risk tier.

## 2. Vision and Goals

**Vision:** No on-call engineer should have to start an investigation from a blank dashboard. By the time a human looks at an incident, the evidence should already be gathered and a root cause hypothesis should already be formed. Where it's safe, the fix should already be applied.

**Goals:**

- **G1 — Close the loop end to end** *(full realization in V1.5)*. Don't stop at "here's what's probably wrong." Actually execute safe fixes and propose risky ones with clear reasoning.
- **G2 — Ground every claim in real evidence** *(MVP)*. No agent output should be an unverified guess. Root cause claims must cite a specific metric, log line, or diff.
- **G3 — Make safety the default, not an afterthought** *(V1.5)*. Every remediation action is tiered by risk. Nothing that could cause irreversible harm happens without a human clicking approve.
- **G4 — Make every agent decision auditable** *(MVP, extended in V1.5)*. A human should always be able to answer "why did the system do that?" after the fact, in full detail.
- **G5 — Run without heavyweight cloud dependency** *(MVP)*. The system should be demonstrable on a single laptop using a local Kubernetes cluster and a mix of free and open-source model providers.

## 3. Target Users and Personas

### Persona 1: Priya — On-Call Backend Engineer (primary user)

Three years of experience, rotates through on-call once every six weeks. Her biggest frustration isn't fixing problems, it's the twenty minutes she spends every time just figuring out *what* is even broken before she can start fixing it. In MVP, she wants the evidence and a hypothesis waiting for her. In V1.5, she additionally wants safe fixes already applied and risky ones queued for a one-tap approval.

### Persona 2: Marcus — Engineering Manager / Incident Commander (secondary user, V1.5-relevant)

Doesn't write the fix, but is accountable for communicating incident status to leadership and customer support. Wants plain-English status updates generated automatically (a V1.5 feature via the Communication Agent) and wants confidence that nothing the system does could make an incident worse without his team's knowledge.

### Persona 3: Dana — Platform/Infrastructure Lead (secondary user, both phases)

Owns the reliability of the underlying platform. In MVP, cares whether the RCA reasoning is actually grounded and accurate, not just confident-sounding. In V1.5, cares additionally about the auditability and safety boundaries of any autonomous action, and wants the ability to kill the system's autonomy instantly.

## 4. User Pain Points

| Pain point | Who feels it | Phase this is addressed in |
|---|---|---|
| Alert fatigue from noisy, uncorrelated alerts | Priya | MVP |
| Context-switching across 6+ dashboards mid-incident | Priya | MVP |
| Tribal knowledge locked in Slack threads and people's memory | Priya, new hires | MVP (RAG over runbooks/postmortems) |
| No time to write postmortems, so lessons aren't captured | Marcus, Dana | V2 stretch (auto-postmortem) |
| Stakeholders get no updates until the engineer resurfaces | Marcus | V1.5 |
| No visibility into whether automation could take a bad action | Dana | V1.5 |
| Same root cause recurring because nobody connected the dots | Dana | V1.5 (memory) |

---

# PART A — MVP SCOPE (Investigation Only, Read-Only)

MVP proves the reasoning is trustworthy before any autonomous action is allowed to exist. Nothing in this phase writes to or modifies production infrastructure. Everything is read-only against Kubernetes, Prometheus, and GitHub.

## 5A. Core Features (MVP)

- Alert ingestion and deduplication (Triage Agent)
- Multi-source evidence correlation across logs, metrics, traces, and deploys (Correlation Agent)
- Root cause hypothesis generation with a cited-evidence requirement and an ensemble confidence score (RCA Agent)
- Runbook and postmortem retrieval grounding (RAG layer, seeded with real postmortems)
- Claim validation against cited evidence (Observer Agent, RCA-scope only)
- Live investigation dashboard (Next.js)
- Replay mode for stepping through resolved incidents

## 6A. User Journeys (MVP)

### Journey A1 — 2 AM page, investigation only

1. `checkout-service` starts erroring. An alert fires and Aegis creates an incident automatically.
2. Priya's phone buzzes with the page as usual. The Slack channel already has a Correlation Agent summary posted: recent deploy, relevant log lines, affected metric graphs, all linked.
3. By the time Priya opens her laptop, the RCA Agent has already posted a hypothesis with a confidence score and the specific evidence it's based on.
4. Priya applies the fix herself, using the evidence and hypothesis as her starting point instead of her usual blank-dashboard investigation.

### Journey C — Exploring replay mode

1. A new team member, or an interviewer evaluating the project, opens the dashboard and picks a resolved incident from the list.
2. Replay mode steps through exactly what each agent saw and decided, in order, at whatever pace the viewer wants.

## 7A. Functional Requirements (MVP)

**FR-1: Alert Ingestion (Triage Agent)**
- FR-1.1: The system must accept incoming alerts via a webhook endpoint.
- FR-1.2: The system must deduplicate alerts referencing the same underlying incident within a configurable time window (default 5 minutes).
- FR-1.3: The system must assign a severity level (P1-P4) based on alert content and affected service criticality.

**FR-2: Evidence Correlation (Correlation Agent)**
- FR-2.1: The system must gather logs, metrics, and traces relevant to the affected service and time window.
- FR-2.2: The system must identify recent deploys or config changes within a configurable lookback window (default 2 hours).
- FR-2.3: The system must correlate across at least two dimensions (temporal and topological) before handing off to RCA.

**FR-3: Root Cause Analysis (RCA Agent)**
- FR-3.1: The system must run a configurable number of ensemble reasoning passes (default 3) and compute an agreement score.
- FR-3.2: Every root cause claim must cite a specific piece of evidence gathered by the Correlation Agent. Claims without a citation must be rejected by the Observer Agent.
- FR-3.3: The system must retrieve relevant runbooks and past incident summaries via RAG before forming a conclusion.

**FR-8 (partial): Observer Agent, RCA scope**
- FR-8.1: The system must validate every RCA claim against the cited evidence before it is surfaced to a human.
- FR-8.2: The system must log every LLM call (prompt, response, latency, token cost, model used) for later audit, from day one.

**FR-9: Replay Mode**
- FR-9.1: The system must allow a user to select any resolved incident and step through its full agent decision history in order.
- FR-9.2: Replay must not require the original infrastructure state; it replays from the persisted audit log.

## 8A. Non-Functional Requirements (MVP)

| Category | Requirement |
|---|---|
| Performance | P95 time from alert ingestion to first RCA hypothesis posted: under 3 minutes |
| Availability | The system must degrade gracefully if a single MCP server is unreachable |
| Security | All MCP tool access in MVP is read-only; least-privilege k8s RBAC (get/list/describe only, no write verbs) |
| Cost | Operable within free-tier LLM API budgets |
| Portability | Runs on a single laptop using a local Kubernetes cluster (`kind`) |
| Maintainability | Every architectural decision of consequence is documented in ESD.md at the time it's made |

## 9A. Success Metrics (MVP)

| Metric | Target |
|---|---|
| RCA accuracy against hand-crafted eval set | ≥ 85% |
| Agent hallucination rate (claims without valid citation) | < 5% |
| Time-to-first-hypothesis | ≤ 3 minutes P95 |
| Replay mode reliability | 100% of resolved incidents replayable without error |

## 10A. Edge Cases (MVP)

- Two incidents fire simultaneously for related services; evidence must not be conflated between them.
- An MCP tool call times out or errors mid-investigation; the agent must degrade gracefully (skip that evidence source, note the gap) rather than stall.
- Ensemble RCA passes disagree significantly; low agreement score must be surfaced clearly rather than silently averaged away.
- The Kubernetes cluster itself is degraded; the system must flag that its own infrastructure is compromised rather than silently failing or fabricating data.

## 11A. Acceptance Criteria (MVP)

**Alert Ingestion**
- Given a webhook payload matching a supported alert schema, when it's received, then an incident record is created within 1 second.
- Given two alerts referencing the same service within the dedup window, when the second arrives, then it is merged into the existing incident.

**Evidence Correlation**
- Given an incident, when the Correlation Agent runs, then it returns logs, metrics, and a deploy-history check, or explicitly notes which sources were unavailable.

**Root Cause Analysis**
- Given correlated evidence, when the RCA Agent runs, then it returns a hypothesis, a confidence score, and at least one cited piece of evidence per claim.
- Given ensemble passes with an agreement score below a configured threshold, when RCA completes, then the incident is flagged as low-confidence rather than presented with false certainty.

**Observer / Audit**
- Given any RCA claim, when the Observer Agent checks it, then a claim without a valid, matching citation is rejected and the RCA Agent is asked to revise or lower its confidence.

**Replay Mode**
- Given a resolved incident, when a user opens replay mode, then every step is viewable in order using only the persisted audit log, with no live infrastructure required.

---

# PART B — V1.5 SCOPE (Safe Autonomous Action)

V1.5 is additive on top of a working MVP. It introduces the ability to act, always inside explicit safety tiers, and introduces the two agents whose job is communication and safety oversight of that acting.

## 5B. Core Features (V1.5)

- Tiered autonomous remediation (Resolution Agent): Tier-1 auto-executes, Tier-2 proposes and waits for approval, Tier-3 never auto-executes
- Human-in-the-loop approval workflow
- Plain-English status updates to Slack and the dashboard (Communication Agent)
- Cross-incident long-term memory with human-approved write-back (Mem0)
- Full agent-decision audit trail extended to remediation actions
- Kill switch and per-service rate limiting on autonomous actions

## 6B. User Journeys (V1.5)

### Journey A2 — 2 AM page, closed loop

Continues from Journey A1: if the fix is Tier-1 (say, restarting a crashed pod), it's already applied and metrics are already recovering by the time Priya opens her laptop. If it's Tier-2 (say, rolling back a deploy), she sees a clear proposal with reasoning and taps approve from her phone. Total time from page to resolution: minutes, not the better part of an hour.

### Journey B — Reviewing a Tier-2 approval

1. A push notification and Slack message arrive: "Proposed fix: roll back deploy `a1b2c3d` for `checkout-service`. Reasoning: [evidence]. Confidence: 82%."
2. The approver opens the dashboard, sees the full evidence trail the RCA Agent used, not just the conclusion.
3. They approve, reject, or request more investigation.
4. If approved, the Resolution Agent executes and the dashboard updates live.

### Journey D — Reading a Communication Agent update

1. Marcus is in a customer call when an incident starts.
2. He gets a plain-English Slack update with no jargon: what's affected, what's being done, an ETA if one can be estimated.
3. He doesn't need to interrupt Priya to ask "what's the status."

## 7B. Functional Requirements (V1.5)

**FR-4: Tiered Remediation (Resolution Agent)**
- FR-4.1: Every possible remediation action must be pre-classified into Tier-1 (auto-execute), Tier-2 (propose, require approval), or Tier-3 (human-only).
- FR-4.2: Tier-1 actions must be rate-limited per service (default: 3 per hour), after which further actions of that type are forced to Tier-2.
- FR-4.3: Every remediation action, regardless of tier, must be logged with full reasoning before execution.
- FR-4.4: The system must estimate blast radius (dependent services affected) before proposing or executing any action.

**FR-5: Human Approval Workflow**
- FR-5.1: A human must be able to approve or reject a Tier-2 proposal from the dashboard or a linked Slack message.
- FR-5.2: A rejected proposal must not be automatically retried; it must be flagged for manual investigation.
- FR-5.3: The system must expose a kill switch, accessible only to authenticated users, that immediately halts all autonomous action across all in-flight incidents.

**FR-6: Communication (Communication Agent)**
- FR-6.1: The system must post a plain-English status update to Slack within 2 minutes of incident creation.
- FR-6.2: Updates must be posted at defined transition points: incident opened, root cause identified, remediation proposed/executed, incident resolved.

**FR-7: Memory**
- FR-7.1: On incident resolution, the system must generate a structured summary (symptom, root cause, fix, outcome).
- FR-7.2: A human must approve or edit the summary before it is written to long-term memory.
- FR-7.3: Memory retrieval must be scoped by both service and incident type (compound key) to avoid cross-contamination between unrelated failure domains.

**FR-8 (remainder): Observer Agent, remediation scope**
- FR-8.3: The system must validate remediation proposals against the Tier-1 allowlist and blast-radius estimate before allowing auto-execution.

## 8B. Non-Functional Requirements (V1.5)

| Category | Requirement |
|---|---|
| Safety | No Tier-2 or Tier-3 action executes without explicit human approval, under any circumstances, including LLM provider fallback |
| Security | No agent process ever holds raw infrastructure credentials directly; write-capable credentials are injected only at the MCP server boundary, scoped to Tier-1 actions |
| Observability | Every agent decision, including every remediation action, must be traceable end to end with no gaps in the audit log |

## 9B. Success Metrics (V1.5)

| Metric | Target |
|---|---|
| MTTR reduction vs. fully manual baseline | ≥ 70% |
| False-positive auto-remediation rate (Tier-1 fixes needing rollback) | < 2% |
| Time-to-first-communication-update | ≤ 2 minutes from incident creation |
| Kill switch response time | < 1 second from trigger to all agents halted |

## 10B. Edge Cases (V1.5)

- A human doesn't respond to a Tier-2 approval request within a defined SLA; the incident must escalate (e.g., page a secondary on-call) rather than silently wait forever.
- The local model produces a nonsensical or unsafe-sounding remediation suggestion; the Observer Agent and the static Tier-1 allowlist must both catch this.
- A bad root cause gets accidentally written to long-term memory; because writes require human approval, this should be rare, but the memory store must support manual deletion/correction.
- The Tier-1 rate limit is hit during a genuine cascading failure; forced escalation to Tier-2 must not slow response so much that it defeats the purpose.

## 11B. Acceptance Criteria (V1.5)

**Tiered Remediation**
- Given a Tier-1 action, when the Resolution Agent decides to act, then the action executes without human input and is logged before execution completes.
- Given a Tier-2 action, when the Resolution Agent proposes it, then no execution occurs until an approval record exists.
- Given a Tier-3 scenario, when the Resolution Agent evaluates it, then no auto-execution path is even offered as an option.
- Given the per-service Tier-1 rate limit is reached, when another Tier-1-eligible action would fire, then it is instead routed to Tier-2.

**Human Approval**
- Given a pending Tier-2 proposal, when a human clicks approve, then the Resolution Agent executes within 5 seconds.
- Given a pending Tier-2 proposal, when a human clicks reject, then the action is marked rejected and the incident is flagged for manual investigation, with no automatic retry.

**Communication**
- Given an incident transitions state, when that transition occurs, then a Slack update is posted within 2 minutes.

**Memory**
- Given a resolved incident, when the summary is generated, then it is not written to long-term memory until a human approves or edits it.
- Given a new incident, when memory is queried, then only entries matching both the service and incident-type scope are returned.

**Kill Switch**
- Given the kill switch is triggered, when any agent attempts a subsequent action, then that action is blocked and logged as blocked, across all in-flight incidents, within 1 second.

---

## 12. Assumptions and Constraints (Both Phases)

- Built and maintained by a single engineer on a 1-2 month timeline: MVP first, V1.5 second.
- Budget-constrained: relies on free-tier and open-source/local LLM providers.
- Runs against a local Kubernetes cluster (`kind`) rather than a real production cloud environment.
- PagerDuty integration is mocked via a fixture-replay engine; Kubernetes, Prometheus, and GitHub integrations are real.
- English-language only. Single-tenant. The hand-crafted eval set (20-30 cases) is a development aid, not a statistically rigorous benchmark.

## 13. Risks and Mitigations (Both Phases)

| Risk | Phase | Impact | Mitigation |
|---|---|---|---|
| A local/free-tier LLM gives a confidently wrong RCA hypothesis | MVP | Wastes engineer time chasing the wrong lead | Ensemble passes + agreement score; Observer citation checking |
| A local/free-tier LLM gives a confidently wrong remediation decision | V1.5 | Could apply a harmful or useless fix | Hard Tier-1 allowlist of only reversible, low-blast-radius actions; per-service rate limiting |
| Free API rate limits are hit mid-demo | Both | Live demo could stall | Local model fallback for every agent role; caching |
| Hand-crafted eval set is too small to be meaningful | MVP | RCA accuracy metric could be misleading | Supplement with real postmortem replay from public incident corpora |
| Scope creep given the ambitious V2 feature list | V1.5→V2 | Nothing ships fully finished | Locked phase gates: V1.5 must be solid before any V2 work starts |
| Auto-remediation erodes trust after one bad Tier-1 action | V1.5 | Users stop trusting even safe automation | Full audit trail and kill switch always visible; false-positive rate tracked as a first-class KPI |

## 14. Future Roadmap

**Now (MVP, build first):** Section 5A-11A above.
**Next (V1.5, build second):** Section 5B-11B above.
**V2 stretch (attempted after V1.5 is solid, not guaranteed):** Automated postmortem generation from the full incident transcript.
**Documented but not built this cycle:** Predictive incident prevention via anomaly detection; expanded auto-remediation tiers; multi-tenant support with RBAC; additional cloud provider integrations; real PagerDuty/Opsgenie/ServiceNow integrations; chaos-engineering automation (Chaos Mesh/Litmus); mobile-native approval flow; SSO/OIDC.

---

## 15. Architecture Review Addendum (Production-Readiness Requirements)

The following requirements came out of a formal senior-architect review of the design and are binding on both phases. They exist to close the gap between "demoable" and "the kind of system a platform team would actually trust."

### 15.1 New / Revised Functional Requirements

**FR-10: Idempotent Ingestion** *(MVP)*
- FR-10.1: Every incoming alert must carry a source-provided external ID. Duplicate deliveries of the same external ID must never create a second incident record.

**FR-11: Idempotent, Leased Remediation Execution** *(V1.5)*
- FR-11.1: Every remediation action must carry an idempotency key and a defined compensating (undo) action before it is allowed to execute, at any tier.
- FR-11.2: Before any Tier-1 or Tier-2 action executes against a target resource, the system must acquire an exclusive lease on that resource identity. A second incident targeting the same resource must wait or be routed to Tier-2 for human coordination.
- FR-11.3: On worker restart after a crash, the system must reconcile actual infrastructure state against the last known action status before retrying anything, rather than blindly re-executing.

**FR-12: System-Wide Action Circuit Breaker** *(V1.5)*
- FR-12.1: In addition to the per-service Tier-1 rate limit (FR-4.2), the system must track Tier-1 executions across all services. If more than a configurable threshold (default 10) execute within a configurable window (default 5 minutes), all further Tier-1 auto-execution must pause system-wide and every in-flight proposal must be escalated to Tier-2 until a human clears the breaker.

**FR-13: Proposal Freshness** *(V1.5)*
- FR-13.1: A Tier-2 proposal not approved within a configurable window (default 10 minutes) must expire and be regenerated with fresh evidence before it can be approved.

**FR-14: Tier-1 Shadow Mode** *(V1.5 rollout gate)*
- FR-14.1: The system must support a shadow mode in which Tier-1 actions are fully reasoned through and logged, including the action that would have been taken, but never actually executed against real infrastructure. Live Tier-1 auto-execution must be a separate, explicit configuration flip made only after a documented shadow-mode burn-in period.

**FR-15: Approval Authorization** *(V1.5)*
- FR-15.1: Only users holding the on-call-engineer role or above may approve or reject a Tier-2 proposal. Viewer-role users may see but not act on proposals.

**FR-16: PII Redaction** *(MVP, since RAG ingestion starts in MVP)*
- FR-16.1: Any log line, commit message, or other free-text evidence must pass through a PII redaction step before it is embedded into the vector store or included in any prompt sent to a non-local model provider.

**FR-17: Prompt Injection Resistance** *(MVP)*
- FR-17.1: All retrieved evidence included in an agent prompt must be wrapped in explicit data delimiters. The Observer Agent must screen evidence for instruction-like content and flag or strip it before it can influence an agent's decision.

### 15.2 New / Revised Non-Functional Requirements

| Category | Requirement | Phase |
|---|---|---|
| Reliability | All state-changing operations (ingestion, remediation execution) must be idempotent under at-least-once delivery semantics | MVP / V1.5 |
| Cost control | Every incident has a hard token/cost budget; on breach, the system degrades (fewer ensemble passes, cheaper model) rather than failing open or exceeding budget | MVP |
| Data protection | No raw PII may be persisted in the vector store or sent to a non-local model provider | MVP |
| Auth security | Session tokens are never stored in a location readable by JavaScript (no localStorage/sessionStorage) | V1.5 |
| Disaster recovery | The system's single datastore has a documented, tested backup and restore procedure with explicit RPO/RTO targets, even if modest for a demo-scale system | MVP |
| Auditability | The audit log's growth rate and retention policy are explicitly documented and bounded, not left to grow unbounded on the primary OLTP instance | MVP |

### 15.3 New Edge Cases

- **Alert storm from a single systemic cause.** A cluster-wide DNS or node failure can produce dozens of simultaneous per-service alerts. The system must be able to recognize a storm pattern (many alerts, common time window, shared upstream dependency) and correlate it into one parent incident with child symptoms, rather than opening dozens of independent investigations. *(V1.5, ties to FR-12's circuit breaker as the safety backstop if correlation misses it.)*
- **Two incidents targeting the same underlying resource.** Covered by the resource lease in FR-11.2, but explicitly called out here as a scenario that must be tested, not just designed for.
- **A Tier-2 fix is approved but the underlying evidence has since changed.** Covered by FR-13's proposal expiry.

### 15.4 New Success Metrics

| Metric | Target |
|---|---|
| Duplicate-incident rate from webhook retries | 0% |
| Double-execution rate of remediation actions | 0% |
| System-wide circuit breaker false-trigger rate (breaker trips with no real systemic issue) | < 1% |
| PII leakage into vector store (sampled audit) | 0 instances |
