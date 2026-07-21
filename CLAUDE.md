# CLAUDE.md — Aegis Operating Guide

This file is the permanent operating guide for any AI agent (Claude Code or otherwise) working on this repository. Read it before starting any session. It is a living document: it is updated after every feature, not just at the start of the project.

**Current phase:** V1.5 (safe autonomous action), in progress. **MVP is complete** — built, tested (114 tests + eval harness), and verified end-to-end against the live kind cluster on 2026-07-21 (Feature Log Entry 5). V1.5 work (Resolution, Communication, Memory, approvals, kill switch, circuit breakers) is now in scope, each feature shipping with its safety mechanism in the same commit per Section 2.

**Source of truth hierarchy:** PRD.md (what and why) → ESD.md (how, architecturally) → this file (day-to-day working rules). If code and ESD.md ever disagree, ESD.md is updated to match the code only if the deviation was a deliberate, reasoned improvement; otherwise the code is fixed to match ESD.md.

---

## 1. Project Overview

Aegis is a multi-agent incident response system: seven agents (Triage, Correlation, RCA, Resolution, Communication, Observer, Orchestrator) investigate and, in V1.5, safely remediate production incidents on a simulated e-commerce company, Meridian Commerce, running on a local Kubernetes cluster. It is built to a genuine production-engineering bar, not a demo-quality bar, even though it is a solo/portfolio project on a 1-2 month timeline. See PRD.md for full requirements and ESD.md for full architecture.

## 2. Development Philosophy

- **Build MVP completely before touching V1.5.** A half-built V1.5 feature sitting alongside an incomplete MVP is worse than a smaller thing that fully works.
- **Every safety mechanism is load-bearing, not decorative.** Rate limits, resource leases, circuit breakers, and the kill switch are not "nice to have" additions to bolt on later. If a feature involves autonomous action, its safety mechanism ships in the same PR, not a follow-up.
- **Grounded over confident.** An agent output without a citation, or a remediation action without a defined compensating action, does not ship, regardless of how reasonable it sounds.
- **Mandatory recurring architecture review.** Before starting any feature that introduces a new external dependency, a new autonomous action type, a new data flow involving evidence from an untrusted source, or a new auth/authorization surface, stop and run the same critique categories used in the original architecture review (PRD.md Section 15): reliability, safety, security, scalability, operational maturity. Write the findings down, even briefly, before writing implementation code. This project stays production-grade by continuously re-applying that scrutiny, not by having applied it once at the start.

## 3. Coding Standards

- Python: `ruff` for lint and format, type hints required on all function signatures, Pydantic models for every data structure crossing a module or API boundary.
- TypeScript/React: `eslint` + `prettier`, strict mode on, no `any` without an inline comment justifying it.
- No bare `except:` clauses. Catch specific exceptions; let unexpected ones propagate with full context rather than being silently swallowed.
- No `print()` for anything a real system would need to observe later; use the structured logger.
- Every function that touches the database, an MCP server, or an LLM call is `async`.

## 4. Naming Conventions

- Agent names in code match the names in PRD/ESD exactly: `triage`, `correlation`, `rca`, `resolution`, `communication`, `observer`. Do not introduce nicknames or abbreviations in code that don't appear in the docs.
- Database tables and columns: `snake_case`, always. Enum values: lowercase with underscores (`remediation_executed`, not `RemediationExecuted`).
- MCP tool names: `verb_noun` (`restart_pod`, `query_metrics`, `get_recent_commits`), matching the pattern already established in ESD.md Section 3.
- API routes: plural nouns, versioned (`/api/v1/incidents`), matching ESD.md Section 7 exactly. Do not add an endpoint that isn't in ESD.md without updating ESD.md in the same PR.

## 5. Architecture Principles

- Agents never call each other directly; all routing goes through the LangGraph Orchestrator (Supervisor pattern, ESD.md Section 24).
- Agents never hold infrastructure credentials directly; all external action goes through an MCP server with its own narrowly scoped credential (ESD.md Section 16).
- Every state-changing operation is idempotent. If you're implementing something that mutates state, ask "what happens if this runs twice with the same input" before writing the happy path.
- Postgres is the single source of truth. Redis is a cache and nothing may treat it as authoritative.
- No component reasons about the whole system's state; each component reasons only about the incident it's currently handling, correlated via `incident_id`.

## 6. Documentation Standards

- Every new module gets a docstring explaining its role in the pipeline, not just its inputs/outputs.
- Any change to the database schema is reflected in ESD.md Section 6 in the same PR, not after the fact.
- Any change to an API contract is reflected in ESD.md Section 7 in the same PR.
- Any new architectural decision or trade-off gets a row added to ESD.md Section 25.

## 7. Testing Requirements

- New agent logic: unit tests for the deterministic parts (tier classification, agreement-score computation, redaction rules), not just an end-to-end happy-path test.
- New MCP server or tool: a contract test against a fixture, added to CI, before the tool is wired into any agent.
- New external dependency (a new MCP server, a new evidence source): a corresponding fault-injection test (kill it mid-call, feed it malformed data) per ESD.md Section 22.
- New remediation action type: must have a test proving its compensating action actually reverses it, not just that the forward action executes.
- No PR merges with failing tests. No PR merges with a lint failure.

## 8. Git Commit Conventions

- Conventional commits: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.
- Commit body references the relevant FR/NFR number from PRD.md when implementing a specific requirement (e.g., `feat: idempotent alert ingestion (FR-10.1)`).
- One logical change per commit. A commit that both adds a feature and reformats unrelated files gets split.
- PR description includes: what changed, which PRD/ESD sections it implements or touches, and a one-line note on what was added to this file's Feature Log (Section 17).

## 9. File Organization Rules

- Follow the folder structure in ESD.md Section 21 exactly. If a new top-level module is genuinely needed, add it to ESD.md Section 21 in the same PR that introduces it.
- Tests live under `tests/{unit,contract,eval,fault_injection,integration}/`, mirroring the module they test, not colocated with source files. `integration` holds DB-backed tests against the dedicated `aegis_test` database (skipped automatically when Postgres is down).
- No MCP server imports another MCP server's code. They are independently deployable; treat them that way even in local development.

## 10. Refactoring Guidelines

- Refactor within a PR that also does something else only if the refactor is small and directly enables that PR's actual purpose. Otherwise, refactors get their own PR.
- Never refactor a safety mechanism (resource leasing, circuit breaker, kill switch) without adding or updating its corresponding test in the same PR.
- If a refactor changes behavior, not just structure, it needs a `fix:` or `feat:` commit type, not `refactor:`.

## 11. Performance Expectations

- P95 time-to-first-hypothesis under 3 minutes (PRD.md 9A). If a change measurably regresses this, it needs a justification in the PR description.
- Every incident has a hard token/cost budget (ESD.md Section 15). New agent logic that adds LLM calls must account for this budget, not treat it as unlimited.
- SSE for anything the frontend needs live; never introduce a polling loop as a shortcut.

## 12. Security Expectations

- No credential, of any kind, is ever committed to the repository, hardcoded, or logged, even in a debug statement.
- No session token is ever stored anywhere JavaScript can read it (ESD.md Section 8). This is non-negotiable, not a style preference.
- Any new MCP server gets the narrowest possible RBAC scope for what it actually needs, spelled out explicitly in ESD.md Section 16, not granted broadly "to be safe for later."
- Any new evidence source that could contain untrusted free text (logs, commit messages, external API responses) goes through the redaction pipeline and is wrapped in explicit data delimiters before it reaches a prompt (ESD.md Section 16).
- Any new approval-gated or destructive action is checked server-side for the correct role, never trusted from client-side state alone.

## 13. Definition of Done

A feature is done when, and only when:
1. It implements its corresponding PRD.md FR/NFR, referenced by number.
2. It has unit tests for deterministic logic and a contract test for any new external tool.
3. It passes CI: lint, unit tests, contract tests.
4. Its safety mechanism (if it involves autonomous action) shipped in the same PR, not a follow-up.
5. ESD.md is updated if the schema, API surface, or architecture changed.
6. This file's Feature Log (Section 17) has a new entry.
7. No secrets, no bare excepts, no `any` without justification.

## 14. Rules for Adding New Features

1. Check PRD.md: is this feature actually in scope for the current phase (MVP vs V1.5)? If not, don't build it yet, even if it seems easy.
2. Run the mandatory architecture review (Section 2) if the feature introduces new external dependencies, new autonomous actions, new untrusted data flows, or new auth surfaces.
3. Update ESD.md with the design before writing implementation code for anything non-trivial (new module, new table, new API contract).
4. Implement with tests in the same PR, not after.
5. Update this file's Feature Log on completion.

## 15. Rules for Modifying Existing Code

- Don't change an agent's tier classification, a safety threshold (rate limits, circuit breaker thresholds, proposal expiry windows), or an RBAC scope without calling it out explicitly in the PR description, since these are safety-relevant even when the code change looks small.
- Don't silently change a database column's meaning; add a new column and migrate, don't repurpose.
- If you're touching code with no test coverage, add coverage for the part you're touching as part of the change, don't just patch around the gap.

## 16. Rules for Updating Documentation

- PRD.md changes (new/changed requirements) require a corresponding ESD.md check: does the architecture already support this, or does it need a design update too?
- ESD.md changes (new/changed architecture) never happen without a corresponding PRD.md requirement driving them. Architecture doesn't grow features on its own.
- This file changes whenever a convention actually changes in practice, not preemptively. Don't document a rule nobody's following yet.

## 17. Common Project Conventions

- Every incident-scoped log line, LLM trace, and span carries `incident_id`. If you're adding an observability point that doesn't, that's a bug.
- Every remediation action type is defined with both its forward action and its compensating action at the same time; there is no such thing as a remediation action without a documented undo.
- Every new evidence source is assumed untrusted until proven otherwise; the default is redaction + delimiting, not an opt-in.

## 18. Important Implementation Constraints

- Local-first: the full system must run on a single laptop against `kind`. Don't introduce a hard dependency on a paid cloud service for core functionality.
- Free-tier LLM budget: default agent-to-model assignments (ESD.md Section 20) are chosen for cost reasons; don't casually upgrade an agent to a more expensive model without checking the budget impact.
- Single Postgres instance: don't introduce a second datastore without updating ESD.md Section 25 with the trade-off reasoning, since the whole design deliberately avoided polyglot persistence.

---

## 19. Mandatory Self-Maintenance Rule

**After implementing every feature, before considering the work done, append a new entry to the Feature Log below.** This is not optional and is not a suggestion to remember to do "when there's time." A feature without a Feature Log entry is not done, per Section 13.

Each entry must include:
- The feature implemented and the PRD.md FR/NFR it satisfies
- Any notable architectural decision made while building it (and whether ESD.md was updated to reflect it)
- Any new convention introduced (and whether Sections 1-18 above were updated to reflect it)
- Any new assumption or constraint surfaced during implementation that wasn't previously documented

## Feature Log (Append-Only)

### 2026-07-20 — Entry 0: Project scaffolding
- PRD.md, ESD.md, and this file created, incorporating the full architecture review (PRD.md Section 15): idempotent ingestion and remediation, resource leasing, per-service and system-wide circuit breakers, proposal expiry, Tier-1 shadow mode, approval RBAC, PII redaction, prompt-injection resistance, JWT-in-httpOnly-cookies, and audit log partitioning.
- Architectural decision: single Postgres instance for all relational and vector data, with the audit log partitioned monthly to manage its disproportionate growth rate. Recorded in ESD.md Section 25.
- Convention introduced: the mandatory recurring architecture review before any feature touching a new external dependency, autonomous action, untrusted data flow, or auth surface (Section 2 above).
- No code written yet. Next entry should correspond to the first real MVP feature (alert ingestion, FR-1/FR-10).

### 2026-07-20 — Entry 1: Rename to Aegis + repo scaffold (Milestone 0)
- Project renamed **IncidentPilot → Aegis** everywhere (PRD.md, ESD.md, this file). Target GitHub repo is now `aegis`.
- Repo scaffolded per ESD Section 21: `backend/`, `frontend/`, `infra/`, `eval/`, `docs/`; backend package `__init__.py` docstrings; `pyproject.toml` (ruff + pytest), `docker-compose.yml` (Postgres/pgvector + Redis), `.gitignore`, `.env.example`, `README.md`. PRD.md/ESD.md moved into `docs/`.
- ESD Section 21 updated to add two new backend modules: `core/` (settings, logging, DB engine) and `providers/` (LLM Strategy abstraction, ESD §20), per Section 9.
- Deliberate deviation (documented in ESD §21 and BUILD_LOG): `CLAUDE.md` stays at repo root, not `docs/`, because the agent tooling loads it there.
- Convention introduced: **`docs/BUILD_LOG.md`** is the detailed parallel build journal; this Feature Log keeps brief pointers into it.
- Constraint surfaced: host Python is 3.14 but LangGraph/pydantic-core wheels may lag — backend venv targets Python 3.12. `kind`/`kubectl`/`helm`/`gh` not yet installed (needed M2/M8).

### 2026-07-21 — Entry 2: Data layer — Postgres + pgvector (Milestone 1)
- Implemented the MVP persistence layer (ESD §6): `core/{config,logging,db}`, `db/enums.py` (StrEnums), `db/state_machine.py` (ESD §6.1 transition allow-list), all MVP ORM models, the repository layer (`db/repository.py`) with idempotent `upsert_incident` (FR-10.1) and validated `record_transition`, and the hand-written Alembic `0001_initial` migration.
- Architectural decision: **native Postgres enums** rather than String+CHECK for `incidents.state` (ESD §6.1) — equivalent guarantee, stronger + idiomatic; all states incl. V1.5 defined now to avoid an enum migration later. `audit_log` created as a monthly range-partitioned table with 13 pre-created partitions (ESD §17); rolling maintenance is a documented ops task. ESD unchanged (schema already specified these).
- Environment: installed Python 3.12.10; `backend/.venv` created on it; all deps install cleanly (resolves the 3.14 wheel risk).
- Tests: 27 unit tests pass (state machine + enum pinning); ruff lint+format clean; `alembic upgrade head` applies the full schema (tables, 13 partitions, HNSW index) against live Postgres+pgvector.

### 2026-07-21 — Entry 3: Local cluster — kind + Meridian + Prometheus (Milestone 2)
- Stood up the local chaos-testing environment (ESD §18): `infra/kind/cluster.yaml` (single-node `aegis`, host 9090→Prometheus, 9093→Alertmanager), Meridian Commerce simulator (`infra/meridian/`, one image → checkout/payment/catalog with `/metrics` + `POST /admin/failure`), kube-prometheus-stack via Helm (`infra/kube-prometheus-stack/values.yaml`, Grafana off), manifests (namespace, deployments/services/ServiceMonitor), and `setup-cluster.sh` + `inject-failure.sh`.
- Safety: k8s MCP RBAC is **read-only** — ServiceAccount `aegis-k8s-mcp` bound to a meridian-namespaced Role with only get/list/watch on pods/logs/events/services/deploys/replicasets; no write verbs (ESD §16, PRD NFR). Verified via `kubectl auth can-i` (delete/patch/secrets → no).
- Ran the CLAUDE.md §2 architecture review (new external dep: k8s + Prometheus) — reliability/safety/security/scalability notes in BUILD_LOG.
- Tooling installed: kind 0.32.0, helm 4.2.3 (in `~/bin`); kubectl 1.34.1 from Docker Desktop.
- Verified end-to-end: 6/6 pods Ready, Prometheus scrapes all 6 targets, injected error spike visible in Prometheus and isolated per service.

### 2026-07-21 — Entry 4: Read-only MCP servers — k8s, Prometheus, GitHub (Milestone 3)
- Implemented the three MVP evidence-source MCP servers (ESD §3, §11, §12; FR-2.1/FR-2.2): `mcp_servers/{k8s,prometheus,github}/{models,client,server}.py` over the official `mcp` SDK (FastMCP, stdio), 11 `verb_noun` tools total, plus shared `mcp_servers/common.py` (`ToolResult` envelope, transient-only exponential-backoff retry ×3, `guarded` degradation — a tool never raises across the MCP boundary, it returns `ok=false` with a machine-readable `error_kind`).
- Ran the CLAUDE.md §2 architecture review first (new external dep: GitHub API; new untrusted flows: logs/events/commit text) — findings in BUILD_LOG M3. ESD §3 updated in the same change with the full MVP tool inventory table.
- Convention introduced: every `ToolResult` carries **`contains_untrusted_text`**; any output flagged true must pass the (future, consumer-side) redaction + `<evidence>` delimiting middleware before reaching a prompt. Also: `common.py` is a sibling library, permitted by §9 (which forbids importing *another server's* code).
- Security: k8s server authenticates with the dedicated `aegis-k8s-mcp` SA token (minted short-lived by `infra/gen-mcp-credentials.sh` into git-ignored files, TLS verified against the cluster CA); GitHub uses optional `GITHUB_TOKEN`; no credential committed or logged.
- Tests: 48 passing (27 prior + contract tests against fixtures for all three servers + fault-injection: connection-kill/timeout/repeated-5xx → `unavailable` after exactly 3 attempts, malformed JSON → fail-fast `malformed_response`, 404 → `not_found`, recovery after upstream returns, backoff sequence asserted). Live-verified against the real cluster (SA token path), real Prometheus, and a real public GitHub repo.
- Constraint surfaced: SA tokens from `kubectl create token` expire (24h default) — each session needs a re-mint; kind's API-server host port changes if the cluster is recreated (script prints the current `K8S_API_URL`).

### 2026-07-21 — Entry 5: MVP complete — M4 redaction, M5 API, M6 agents/orchestrator/worker/RAG, M7 eval, M8 frontend
- **M4** (FR-16/ESD §16): deterministic PII redaction (Luhn-checked cards; bearer-before-assignment ordering) + `<evidence>` delimiting with embedded-tag defang; the single middleware before embed/cache/log/prompt.
- **M5** (FR-1.*, FR-9, FR-10.1, ESD §7/§8/§12): ingestion with idempotency + dedup window + deterministic severity; JWT httpOnly-cookie auth with refresh rotation and family-revocation on reuse (`refresh_sessions`, migration 0002); Postgres LISTEN/NOTIFY → SSE; replay endpoint; error envelope. New `tests/integration/` convention (§9 updated) against auto-migrated `aegis_test`.
- **M6** (FR-2.*, FR-3.*, FR-8.1/8.2, ESD §4/§10/§24): LangGraph supervisor (triage→correlation→rca→observer, one bounded revision); evidence enters only through the redacting `EvidenceStore`; worker speaks **real MCP over stdio** (no infra credentials in the worker process); ensemble RCA with deterministic agreement scoring; deterministic LLM-free observer; hashing-embedder RAG + `/runbooks/search`. Deviation logged in ESD §25: no LangGraph Postgres checkpointer in MVP (read-only idempotent runs; re-run on crash), revisit at V1.5.
- **M7** (ESD §22, PRD 9A): 12-scenario eval corpus + CI harness enforcing accuracy ≥85% (12/12) and hallucination <5% (0). Caught two real keyword-precision bugs.
- **M8** (ESD §5, FR-9): Next.js 15 dashboard (all-incident SSE — endpoint added to ESD §7), live incident view with observer-validated citations, replay stepper, cookie-only auth. Deviation: minimal hand-rolled components instead of full shadcn/ui, documented.
- **E2E verified live:** injected 70% error rate on checkout-service in kind → webhook → 4-agent investigation over real MCP (real logs/events/metrics; GitHub 409 degraded to a documented gap) → observer-validated hypothesis in seconds → dashboard/live view/replay all rendering; dedup merged a second alert; failure cleared.
