# ESD.md — Aegis Engineering Specification Document

**Status:** Living document. MVP and V1.5 specified in full. V2 stretch noted where relevant.
**Audience:** An engineer picking this up cold should be able to implement it with minimal ambiguity.
**Companion documents:** PRD.md (what and why), CLAUDE.md (how work gets done day to day)

This document incorporates the findings of the formal architecture review recorded in PRD.md Section 15. Every mechanism below that closes one of those findings is marked **[review fix]**.

---

## 1. Overall System Architecture

Aegis is a FastAPI backend fronting an async-worker-driven LangGraph agent pipeline, backed by a single Postgres instance (relational data + pgvector), with tool access to real infrastructure mediated exclusively through MCP servers. The frontend is a Next.js dashboard consuming a Server-Sent Events stream for live incident updates.

**MVP** runs this pipeline in read-only investigation mode: Triage → Correlation → RCA, with the Observer Agent validating every RCA claim. No MCP server used in MVP holds write credentials.

**V1.5** adds the Resolution and Communication agents, write-capable (but tightly RBAC-scoped) Kubernetes access, the human approval workflow, Mem0-backed memory, and the safety machinery (resource leases, circuit breakers, kill switch) required to make autonomous action safe.

## 2. High-Level Architecture Diagram

```
                    ┌───────────────────────────────────────────────┐
                    │                  Alert Sources                   │
                    │  Real: Prometheus Alertmanager, GitHub webhooks  │
                    │  Mocked: PagerDuty fixture-replay engine         │
                    └────────────────────┬──────────────────────────┘
                                         │ webhook, carries external_alert_id
                                         ▼
                    ┌───────────────────────────────────────────────┐
                    │                 FastAPI Backend                   │
                    │  Ingestion API (idempotent upsert)                │
                    │  Incident / Approval / Kill-Switch / Circuit-     │
                    │    Breaker API                                    │
                    │  SSE stream endpoint                              │
                    │  Auth: JWT in httpOnly cookies, RBAC              │
                    └────────────────────┬──────────────────────────┘
                                         │ enqueue LangGraph run
                                         ▼
                    ┌───────────────────────────────────────────────┐
                    │              Async Worker Pool                    │
                    │  Postgres-backed LangGraph checkpointer            │
                    │  (durable, resumable across worker restarts)       │
                    │                                                     │
                    │   ┌─────────────────────────────────────────┐    │
                    │   │      LangGraph Orchestrator                 │    │
                    │   │      (hierarchical supervisor)              │    │
                    │   │                                              │    │
                    │   │  Triage → Correlation → RCA (ensemble x3)  │    │
                    │   │       ↑ every step checked by Observer ↓   │    │
                    │   │  [V1.5] Resolution ←→ Communication         │    │
                    │   │       ↑ resource lease + circuit breaker    │    │
                    │   │         gate every Resolution action        │    │
                    │   └───────────────────┬─────────────────────┘    │
                    └───────────────────────┼──────────────────────────┘
                                            │ MCP protocol tool calls
            ┌───────────────────┬──────────┼──────────┬───────────────────┐
            ▼                   ▼                     ▼                   ▼
  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
  │ K8s MCP Server    │ │ Prometheus MCP   │ │ GitHub MCP        │ │ Slack MCP [V1.5]  │
  │ own scoped        │ │ Server            │ │ Server             │ │ Server             │
  │ ServiceAccount     │ │                   │ │                    │ │                    │
  └────────┬───────────┘ └────────┬───────────┘ └────────┬───────────┘ └────────┬───────────┘
           ▼                      ▼                       ▼                     ▼
  ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
  │ local kind cluster│ │ kube-prometheus- │ │ GitHub API         │ │ Slack API           │
  │ Meridian Commerce  │ │ stack (real)     │ │ (real repo data)   │ │                     │
  │ services            │ └─────────────────┘ └─────────────────┘ └─────────────────┘
  └─────────────────┘
                                                          ┌─────────────────┐
                                                          │ PagerDuty MCP     │
                                                          │ (fixture-replay)  │
                                                          └─────────────────┘

                    ┌───────────────────────────────────────────────┐
                    │                    Postgres                       │
                    │  incidents · incident_state_transitions           │
                    │  agent_steps · agent_messages · evidence_citations│
                    │  remediation_actions · resource_leases            │
                    │  action_circuit_breaker_events · approvals         │
                    │  runbooks (pgvector, content-hashed)               │
                    │  memory_summaries · users                          │
                    │  audit_log (monthly partitions)                    │
                    │  langgraph_checkpoints                             │
                    └───────────────────────────────────────────────┘

                    ┌───────────────┐        ┌───────────────┐
                    │ Redis            │        │ LangSmith        │
                    │ prompt cache      │        │ LLM tracing        │
                    │                   │        │ (external SaaS)    │
                    └───────────────┘        └───────────────┘

                    ┌───────────────────────────────────────────────┐
                    │            Next.js Frontend (shadcn/ui)           │
                    │  Dashboard · live incident feed (SSE)             │
                    │  Approval UI [V1.5] · Replay mode · Kill switch   │
                    └───────────────────────────────────────────────┘
```

## 3. Module Breakdown

### Part A — MVP Modules

```
backend/
  ingestion/          alert webhook handling, idempotent upsert
  agents/triage/       severity classification, dedup
  agents/correlation/  evidence gathering across logs/metrics/deploys
  agents/rca/          ensemble reasoning, structured output, citations
  agents/observer/     citation validation, prompt-injection screening
  rag/                 embedding, retrieval, content-hash versioning
  redaction/           PII scrubbing pipeline [review fix]
  mcp_servers/k8s/           read-only in MVP: get/list/describe/logs
  mcp_servers/prometheus/    query metrics, list alerts
  mcp_servers/github/        commits, PRs, diffs
  orchestrator/         LangGraph graph definition, checkpointer wiring
  worker/               async worker pool entrypoint
  api/                  FastAPI routers, auth (read-only endpoints)
frontend/
  app/dashboard/        incident list
  app/incidents/[id]/   live investigation view
  app/replay/[id]/      replay mode
```

### Part B — V1.5 Additions

```
backend/
  agents/resolution/    tiered decision logic, action execution
  agents/communication/ Slack + dashboard update generation
  memory/                Mem0 integration, human-approval gate on writes
  safety/resource_lease/       Postgres advisory-lock based leasing [review fix]
  safety/circuit_breaker/      per-service + global mass-action breaker [review fix]
  safety/kill_switch/          immediate halt across all in-flight incidents
  mcp_servers/k8s/             write verbs added: delete pod, patch deployment replicas
  mcp_servers/slack/
  mcp_servers/pagerduty_mock/  fixture-replay engine
  api/                          approval endpoints, kill switch, circuit-breaker status
frontend/
  app/approvals/         Tier-2 approval queue
  app/settings/kill-switch/
```

## 4. Backend Architecture

FastAPI serves two roles: a synchronous API for ingestion, querying, and human actions, and an enqueue point for asynchronous LangGraph runs. The API process itself never runs a LangGraph graph inline; it writes an incident row and enqueues a run, then returns immediately. A separate async worker pool (asyncio-based, one or more OS processes) pulls pending runs and drives them via LangGraph, using a Postgres-backed checkpointer for durability. **[review fix]** On worker startup, before pulling new work, each worker reconciles any run it had previously claimed but not marked complete, checking actual infrastructure/action state before deciding whether to resume, skip, or retry a step, rather than blindly re-executing.

All agent nodes are pure functions of `(incident_state) -> (updated_state, next_node)`. Side effects (MCP tool calls, database writes) are isolated at defined points so that a given node's reasoning is testable in isolation from its effects.

## 5. Frontend Architecture

Next.js (App Router) with shadcn/ui components. The dashboard subscribes to `/api/v1/incidents/{id}/stream` (SSE) for live updates rather than polling, reducing backend load and giving true real-time feel. Auth state is derived from the httpOnly session cookie; the frontend never reads or stores the JWT directly, it relies on the browser sending the cookie automatically and a `/api/v1/auth/me` endpoint to learn the current user's identity and role. Approval actions (V1.5) are gated client-side by role as a UX convenience only, the real enforcement is server-side (Section 8).

## 6. Database Schema and Relationships

Single Postgres instance, pgvector extension enabled. All tables live in one database; the audit log is partitioned separately from the OLTP-heavy tables to keep it from degrading query performance elsewhere as it grows. **[review fix]**

**incidents**
`id (uuid, pk) · external_alert_id (text) · alert_source (text) · UNIQUE(alert_source, external_alert_id) · title · service_name · severity (enum P1-P4) · state (enum, see 6.1) · created_at · updated_at · resolved_at`

**incident_state_transitions**
`id · incident_id (fk) · from_state · to_state · actor_type(agent|human) · actor_id · created_at`

**agent_steps**
`id · incident_id (fk) · agent_name · ensemble_pass_index (nullable) · input_summary · output_summary · structured_output (jsonb) · confidence · model_used · tokens_used · cost_usd · latency_ms · created_at`

**agent_messages**
`id · incident_id (fk) · agent_name · message_type(reasoning|action|handoff) · content · metadata (jsonb) · created_at`

**evidence_citations**
`id · agent_step_id (fk) · evidence_type(log|metric|diff) · evidence_ref · evidence_snippet_redacted · validated_by_observer (bool)`

**remediation_actions** *(V1.5)*
`id · incident_id (fk) · tier(1|2|3) · action_type · target_resource_type · target_resource_id · idempotency_key (unique) · compensating_action (jsonb) · status(proposed|leased|approved|executed|rejected|failed|rolled_back) · proposed_at · expires_at · approved_by (fk users) · executed_at`

**resource_leases** *(V1.5)* **[review fix]**
`id · target_resource_type · target_resource_id · held_by_remediation_action_id (fk) · acquired_at · released_at`
Partial unique index on `(target_resource_type, target_resource_id) WHERE released_at IS NULL` — enforces exactly one active lease per resource at the database level, not just in application code.

**action_circuit_breaker_events** *(V1.5)* **[review fix]**
`id · window_start · tier1_execution_count · tripped_at (nullable) · cleared_by (fk users, nullable) · cleared_at (nullable)`

**approvals** *(V1.5)*
`id · remediation_action_id (fk) · approver_user_id (fk users) · decision · decided_at · notes`

**runbooks**
`id · title · content · content_hash · source(internal|postmortem_corpus) · source_company (nullable) · service_tags (text[]) · embedding (vector(768)) · ingested_at`
`content_hash` lets the RCA Agent detect a stale citation if the underlying document has changed since it was embedded. **[review fix]**

**memory_summaries** *(V1.5)*
`id · incident_id (fk) · service_name · incident_type · symptom · root_cause · fix · outcome · approved_by (fk users) · created_at`
Composite index on `(service_name, incident_type)` — the compound scoping key from the earlier design correction.

**users**
`id · email · hashed_password · role(admin|on_call_engineer|viewer) · created_at`

**audit_log** *(partitioned monthly, 13-month rolling retention)* **[review fix]**
`id · actor_type(agent|human|system) · actor_id · action · target · incident_id · metadata (jsonb) · created_at`

**langgraph_checkpoints** — schema owned by LangGraph's Postgres checkpointer library; not hand-designed.

### 6.1 Incident State Machine **[review fix]**

```
open → investigating → hypothesis_formed
  → remediation_proposed → remediation_approved → remediation_executed
  → monitoring → resolved → closed
                                   ↖ reopened (from resolved or closed)
```

Every transition is written to `incident_state_transitions` and validated against an explicit allow-list of legal `(from_state, to_state)` pairs at the application layer, backed by a Postgres CHECK constraint on `incidents.state` limiting it to the defined enum values. `remediation_executed` is unreachable from any state except `remediation_approved` (Tier-2/3) or directly from `hypothesis_formed` for Tier-1 (which auto-approves per FR-4.1), enforced in the same transition table.

## 7. API Specifications

All endpoints are versioned under `/api/v1`.

| Endpoint | Method | Phase | Notes |
|---|---|---|---|
| `/incidents` | POST | MVP | Alert ingestion webhook. Idempotent on `(alert_source, external_alert_id)`. |
| `/incidents` | GET | MVP | List incidents, filterable by state/service/severity |
| `/incidents/{id}` | GET | MVP | Full incident detail |
| `/incidents/{id}/stream` | GET | MVP | SSE stream of live updates |
| `/incidents/{id}/replay` | GET | MVP | Full ordered agent-decision history for replay mode |
| `/incidents/{id}/approvals` | POST | V1.5 | Approve/reject a Tier-2 proposal. Requires `on_call_engineer` or `admin` role. |
| `/circuit-breaker/status` | GET | V1.5 | Current state of the global mass-action breaker |
| `/circuit-breaker/clear` | POST | V1.5 | Manually clear a tripped breaker. Requires `admin` role. |
| `/kill-switch` | POST | V1.5 | Immediate halt of all autonomous action. Requires `admin` role. |
| `/auth/login` | POST | MVP | Issues httpOnly access + refresh cookies |
| `/auth/refresh` | POST | MVP | Rotates refresh token, detects reuse |
| `/auth/me` | GET | MVP | Current authenticated user and role |
| `/runbooks/search` | GET | MVP | RAG search over the runbook/postmortem corpus |

## 8. Authentication & Authorization

Self-issued JWT, chosen over a third-party OAuth provider to avoid coupling the project to a specific cloud vendor. **[review fix]** Tokens are stored exclusively in httpOnly, Secure, SameSite=Strict cookies, never in localStorage or any JavaScript-readable location. Access tokens are short-lived (15 minutes); refresh tokens are longer-lived (7 days) and rotated on every use, with reuse of an already-rotated refresh token treated as a signal of token theft and triggering session invalidation for that user.

Roles: `admin`, `on_call_engineer`, `viewer`. **[review fix]** Approval of a Tier-2/3 proposal, clearing the circuit breaker, and triggering the kill switch are all restricted to `on_call_engineer` or `admin`; `viewer` can see everything but act on nothing. Role is checked server-side on every state-changing endpoint; the frontend's role-based UI gating is a convenience layer only.

## 9. Data Flow

1. An alert arrives at `/incidents` with an `external_alert_id`. The row is upserted, not blindly inserted.
2. A LangGraph run is enqueued; a worker picks it up and begins the Triage → Correlation → RCA chain, checkpointing state after every node.
3. Correlation Agent output (raw logs, commit messages) passes through the redaction pipeline before it becomes part of any prompt or is embedded. **[review fix]**
4. Every RCA claim carries a citation; the Observer Agent validates each citation against the actual evidence before the hypothesis is surfaced, and separately screens all evidence text for injected instructions. **[review fix]**
5. *(V1.5)* If remediation is warranted, the Resolution Agent classifies the action's tier, acquires a resource lease on the target, checks the circuit breaker, and only then executes (Tier-1) or proposes (Tier-2/3).
6. *(V1.5)* Communication Agent posts updates at defined state transitions.
7. *(V1.5)* On resolution, a memory summary is generated and held for human approval before being written to long-term memory, scoped by the `(service_name, incident_type)` compound key.
8. Every step, tool call, and decision is written to the audit log with the incident ID as a correlation key, propagated through logs, LangSmith traces, and OpenTelemetry spans alike.

## 10. Component Interactions

The Orchestrator (LangGraph supervisor) is the only component with authority to decide which agent runs next; individual agents do not call each other directly. MCP servers are stateless request/response services; they hold no incident context, only the credentials and logic needed to execute one class of tool call. The Postgres-backed checkpointer is the single durability mechanism for in-flight runs, meaning any worker process can pick up and resume a run that another worker started, which is what makes the reconciliation-on-restart behavior in Section 4 possible.

## 11. Service Boundaries

- **FastAPI API** owns HTTP concerns, auth, and the incident/approval data model. It does not run agent reasoning inline.
- **Async Worker Pool** owns agent execution. It has no HTTP surface of its own.
- **MCP Servers** own translation between the standard tool-call interface and a specific external system's API. Each is independently deployable and independently credentialed.
- **Postgres** is the single source of truth for all state, including LangGraph's own checkpoints. No component maintains authoritative state elsewhere.
- **Redis** is purely a cache. Nothing is allowed to treat Redis as a source of truth; a cold Redis is a performance regression, not a correctness incident.

## 12. Error Handling Strategy

- **MCP tool call failure:** retry with exponential backoff (3 attempts), then mark that evidence source as unavailable and continue the investigation with a documented gap, rather than stalling the whole run.
- **Malformed LLM output:** retry once with a stricter, schema-reinforced prompt; on a second failure, degrade to a cheaper/simpler model or fewer ensemble passes rather than failing the incident outright.
- **Worker crash mid-run:** the Postgres checkpointer preserves state up to the last completed node; on restart, the reconciliation step (Section 4) determines safe resumption.
- **Remediation action failure:** logged as `status = failed`, the pre-registered compensating action is offered to the human rather than assumed to be automatically necessary (a failed action may not need undoing at all). **[review fix]**
- **Global exception handling:** FastAPI returns a structured error envelope (`{error_code, message, incident_id (if applicable)}`) for every 4xx/5xx response; no bare stack traces reach the client.

## 13. Logging and Observability

Every log line, LLM trace (LangSmith), and OpenTelemetry span carries the `incident_id` as a correlation key, making it possible to reconstruct a single incident's full story across every layer of the system. **[review fix]** PII redaction is applied before logging, not only before embedding — the same redaction pass covers both paths, so raw customer data never lands in application logs either. LangSmith captures prompt/response/latency/cost for every LLM call (from MVP onward, per FR-8.2). OpenTelemetry captures infrastructure health (the actual production-adjacent signals: request latency, error rates, resource usage) independently of the AI reasoning layer, which is what the Correlation Agent consumes as raw evidence.

## 14. Caching Strategy

Redis caches two things: identical prompt+context hashes within the same incident run (avoiding redundant LLM calls across ensemble ambiguity or agent retries), and short-TTL (60s) caches of repeated MCP tool calls within a single incident (e.g., the same Prometheus query issued by both Correlation and RCA). Cache keys always include the incident ID to prevent cross-incident leakage. Redaction happens before a value is cached, never after, so cached content is never a route around the redaction pipeline. **[review fix]**

## 15. Performance Considerations

- Ensemble RCA passes run concurrently, not sequentially, to bound total latency.
- SSE instead of polling for the dashboard, reducing backend request volume.
- pgvector HNSW index on the `runbooks.embedding` column for sub-second similarity search.
- **[review fix]** Every incident carries a hard token/cost budget. On approaching the budget, the system degrades gracefully: fewer ensemble passes, a cheaper model, or an early escalation to human review, rather than either silently overspending or failing the incident outright.
- Target: P95 time to first RCA hypothesis under 3 minutes (PRD Section 9A).

## 16. Security Considerations

- **[review fix] Prompt injection resistance:** all retrieved evidence is wrapped in explicit data delimiters within prompts (e.g., `<evidence source="log">...</evidence>`), and the model is instructed that content inside those tags is data, never instructions. The Observer Agent additionally screens evidence text for instruction-like patterns before it's allowed to influence a decision.
- **[review fix] Literal least-privilege RBAC for the Kubernetes MCP server:** a dedicated ServiceAccount with a Role permitting exactly `get`, `list`, `watch`, `delete` on `pods`, and `patch` on `deployments` scoped to the `spec.replicas` field only. No `exec`, no `secrets` access, no `configmaps` write, no cluster-admin binding, ever.
- **[review fix] Per-MCP-server credentials:** each MCP server (Kubernetes, Prometheus, GitHub, Slack) runs with its own dedicated, narrowly scoped credential, mounted directly into that server's own pod. There is no shared "god" credential and no central credential-injection proxy in this phase; that centralized-proxy pattern (as used by larger platforms) is noted as a documented future upgrade, not attempted here, because it would be disproportionate infrastructure for a solo-built system at this scale.
- **[review fix] PII redaction:** a regex- and pattern-based pass strips emails, IP addresses, phone numbers, and card-number-like sequences from any evidence before it is embedded, cached, logged, or sent to any non-local model provider.
- **[review fix] JWT security:** httpOnly/Secure/SameSite=Strict cookies, short-lived access tokens, rotated refresh tokens with reuse detection.
- **[review fix] Approval authorization:** enforced server-side on every state-changing endpoint, not just in the UI.
- Input validation via Pydantic schemas on every API boundary.
- Secrets (API keys, DB credentials, MCP service account tokens) are supplied via environment variables / mounted k8s Secrets, never committed to the repository.

## 17. Scalability Strategy

This is intentionally a modest-scale system by design (single laptop, local cluster), but the scaling path is documented rather than ignored:

- FastAPI and the worker pool are stateless and can scale horizontally behind a load balancer; all shared state lives in Postgres.
- **[review fix]** The `audit_log` table is partitioned by month specifically because it is the fastest-growing table in the system by a wide margin (one row per LLM call, per tool call, per state transition); a 13-month rolling retention policy with older partitions archived to cold storage keeps the primary instance's working set bounded.
- **[review fix]** The system-wide circuit breaker (Section 6, `action_circuit_breaker_events`) exists specifically to prevent a single systemic root cause from producing an unbounded wave of individually-safe-but-collectively-risky autonomous actions, which is the actual scaling risk that matters here, not raw request throughput.
- If vector search volume grows beyond what a single pgvector instance handles well, the documented upgrade path is a dedicated vector store (Weaviate), decoupled from the OLTP database, without changing the RAG interface the agents call.

## 18. Deployment Architecture

Two distinct environments, deliberately kept separate:

1. **Local chaos-testing environment:** a `kind` cluster running Meridian Commerce's simulated services plus `kube-prometheus-stack`. This is where failures are deliberately injected (manual `kubectl delete pod` / resource-limit scripts) to generate realistic incidents, and where Tier-1 remediation actually executes against real (if local) infrastructure.
2. **Public demo environment:** a separate, smaller deployment (e.g., a single small VM or managed platform) hosting the frontend and backend against a *read-only, replayed* incident history, built and validated locally first, then deployed publicly once verified (per the locked Q&A decision to build/test locally before any public exposure).

Both environments build from the same Docker images; only the target infrastructure and MCP server credentials differ.

## 19. Infrastructure Decisions

- `kind` for the local practice Kubernetes cluster, chosen for CI-friendliness and lighter resource footprint than `minikube`.
- `kube-prometheus-stack` for real, standards-based metrics rather than mocked metric data.
- Postgres with `pgvector` as the single datastore, chosen over a polyglot-persistence approach to minimize operational surface area for a solo build, with the documented growth/retention plan (Section 17) as the mitigation for the one real risk that choice creates.
- Redis for caching only, never a source of truth.
- Manual failure-injection scripts rather than a full Chaos Mesh/Litmus deployment for MVP/V1.5, matching the "keep it simple, not deeply familiar with Kubernetes" constraint; Chaos Mesh is documented as a V2+ upgrade.

## 20. Technology Stack with Rationale

| Layer | Choice | Rationale |
|---|---|---|
| Orchestration | LangGraph | Branching, stateful multi-agent graphs with built-in checkpointing |
| Agent framework | LangChain | Model-provider abstraction, tool-calling conventions |
| Backend | FastAPI | Async-native, strong typing via Pydantic, fast to build on |
| Frontend | Next.js + shadcn/ui | SSR/SSG for the public demo, component consistency, Tailwind under the hood |
| Database | Postgres + pgvector | One datastore for relational and vector data, minimal operational surface |
| Cache | Redis | Purpose-built for exactly the caching this system needs |
| Memory | Mem0 | Purpose-built long-term agent memory layer |
| Embeddings | Open-source BGE | No vendor lock-in, no per-embedding cost |
| Tracing | LangSmith | Native to LangGraph/LangChain, fastest integration |
| Infra metrics | OpenTelemetry + Prometheus | Industry-standard, decoupled from the AI reasoning layer |
| Local k8s | kind | Lightweight, CI-friendly practice cluster |
| Auth | Self-issued JWT | No third-party vendor coupling, demonstrates understanding of the mechanism |
| LLM providers | Multi-provider (Claude free tier, Groq, local Ollama models) | Cost-managed, right-sizes model capability to task difficulty per agent |

## 21. Folder/Project Structure

```
aegis/
  backend/
    agents/{triage,correlation,rca,resolution,communication,observer}/
    orchestrator/
    worker/
    api/
    core/                        # settings, structured logging, DB engine/session (cross-cutting)
    providers/                   # LLM provider Strategy abstraction (§20); default deterministic stub
    rag/
    redaction/
    memory/
    safety/{resource_lease,circuit_breaker,kill_switch}/
    mcp_servers/{k8s,prometheus,github,slack,pagerduty_mock}/
    db/{models,migrations}/
    tests/{unit,contract,eval,fault_injection}/
  frontend/
    app/{dashboard,incidents,replay,approvals,settings}/
    components/
    lib/
  infra/
    kind/
    kube-prometheus-stack/
    helm-or-manifests/
    github-actions/
  eval/
    synthetic_incidents/
    postmortem_corpus_ingest/
  docs/
    PRD.md · ESD.md · BUILD_LOG.md
  README.md
  CLAUDE.md                      # kept at repo root (agent tooling loads it there), not under docs/
  docker-compose.yml             # local Postgres (pgvector) + Redis for development
```

## 22. Testing Strategy

- **Unit tests** for all deterministic logic: tier classification, agreement-score computation, redaction patterns, state-machine transition validation.
- **Contract tests** for every MCP server against its fixture set, run in CI on every PR.
- **Eval harness** against the 20-30 hand-crafted synthetic incidents, scoring RCA accuracy against known ground truth, with a minimum threshold enforced in CI.
- **[review fix] Fault-injection tests targeting Aegis's own components**, distinct from the infrastructure chaos-testing used to generate incidents: an MCP server process is killed mid-call, an LLM response is mocked as malformed JSON, the DB connection pool is exhausted — each must produce the documented graceful-degradation behavior from Section 12, not a crash.
- **Frontend component tests** (React Testing Library) and one end-to-end smoke test (Playwright) against the public demo deployment after every deploy.

## 23. CI/CD Considerations

GitHub Actions pipeline: lint (ruff, eslint) → unit tests → contract tests → build Docker images → eval harness (nightly rather than per-PR, to manage token cost) → deploy to `kind` for integration verification → on merge to `main`, deploy to the public demo environment. Fault-injection tests run per-PR since they don't consume LLM tokens.

## 24. Design Patterns Used

- **Supervisor pattern** — LangGraph's hierarchical orchestrator routes every agent transition; agents never call each other directly.
- **Strategy pattern** — LLM provider is swappable per agent via configuration, not hardcoded.
- **Adapter pattern** — every MCP server adapts one external API to the standard MCP tool-call interface.
- **Repository pattern** — all database access goes through a repository layer, not raw queries scattered through agent code.
- **Observer pattern** — literally, the Observer Agent watches every other agent's output; architecturally, also the SSE pub/sub mechanism feeding the frontend.
- **[review fix] Idempotency-key pattern** — applied to both alert ingestion and remediation execution.
- **[review fix] Distributed lease pattern** — Postgres advisory locks / the `resource_leases` table prevent concurrent conflicting actions on the same target.
- **[review fix] Circuit breaker pattern** — applied at two levels: per-service (FR-4.2) and system-wide (FR-12).
- **[review fix] Redaction middleware** — a single pass applied uniformly before logging, caching, embedding, or external-provider transmission of any evidence text.

## 25. Trade-offs and Architectural Decisions

| Decision | Chose | Over | Why |
|---|---|---|---|
| Datastore | Single Postgres + pgvector | Polyglot persistence (dedicated vector DB, separate log store) | Minimizes operational surface for a solo build; the one real risk (audit log growth) is mitigated via partitioning rather than avoided via a second system |
| Credential injection | Per-MCP-server dedicated ServiceAccount | Centralized secret-injection proxy (Envoy-style) | Right-sized for this scale; the enterprise pattern is documented as a future upgrade rather than built prematurely |
| Locking | Postgres advisory locks / lease table | Dedicated distributed lock service (Redis/Zookeeper) | Postgres is already the single source of truth; a second locking system would add complexity without a corresponding benefit at this scale |
| Chaos testing | Manual failure-injection scripts | Chaos Mesh/Litmus | Matches the stated "keep it simple" constraint and limited Kubernetes familiarity; documented as a V2+ upgrade |
| Auth | Self-issued JWT | Firebase/OAuth provider | Avoids vendor coupling in an otherwise vendor-agnostic stack; demonstrates the mechanism rather than delegating it |
| Ensemble RCA cost | 3 concurrent reasoning passes | A single pass | Buys a real, structured agreement-score signal (Section 6.1's structured output) instead of a single unverifiable confident answer; cost is bounded by the per-incident token budget (Section 15) |
