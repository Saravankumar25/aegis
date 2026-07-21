# CLAUDE.md — Aegis Operating Guide

This file is the permanent operating guide for any AI agent (Claude Code or otherwise) working on this repository. Read it before starting any session. It is a living document: it is updated after every feature, not just at the start of the project.

**Current phase:** **V2e.** MVP and V1.5 are complete and verified end-to-end against the live kind cluster (Entries 5 and 6). V2a added Firebase/Google auth and made Redis load-bearing (Entry 8). V2b migrated to a dedicated Firebase project and built real semantic embeddings + a production RAG pipeline (Entry 9). V2c audited the **AI layer** — which turned out to be one real LLM agent out of seven — and began replacing simulated intelligence with genuine reasoning: versioned prompts, enforced structured outputs, streaming, a guardrails layer, LLM Triage (clamped), LLM Communication, and an LLM Observer critique running alongside the deterministic validator (Entry 10). **244 backend tests** pass; ruff clean.

V2d made Correlation a real LLM tool-calling loop and replaced static routing with an LLM supervisor over a genuine graph cycle; every LLM call site now uses the prompt registry, guardrails and `prompt_ref`, enforced structurally in CI (Entry 11). V2e added LangSmith tracing and a deterministic RAG quality gate that runs in CI, plus a wired-but-unexecuted RAGAS harness (Entry 12). **274 backend tests** pass; ruff clean.

**Known-open, tracked, and deliberately NOT claimed as done:**
- **Retrieval has no relevance floor.** `rag_min_score` defaults to `0.0`, so an out-of-domain query returns confident operational runbooks and feeds distractors into the RCA prompt. **Measured, not suspected** — the `unanswerable` golden case fails. Fixing it needs care: cross-encoder scores are unbounded logits, not probabilities.
- **LangSmith is wired but inert** until `LANGSMITH_API_KEY` is set. No trace has ever been emitted.
- **RAGAS metrics are wired but have never been executed** — they need the optional extra *and* a judge model, so **no faithfulness or hallucination number exists yet**. The per-commit gate is the deterministic retrieval suite only.
- **RCA alone does not use `complete_structured()`** — it needs pass-level retry semantics, so it keeps `complete()` + `_parse_pass`. It therefore gets no API-layer schema enforcement.
- **LLM output quality is unverified end-to-end** — blocked on OpenRouter free-tier daily quota, not on code. Degradation paths *were* verified live under a real quota exhaustion.
- **Resolution and Memory remain deterministic.** Resolution is a category→action catalog lookup gated by the four safety gates; Memory recall is a SQL filter. Both are candidates for LLM reasoning, neither has been converted, and the safety gates around Resolution must stay deterministic regardless.
Any further work starts with a fresh architecture review per Section 2.

**Environment constraint worth re-reading before anything else:** the backend venv **must** be Python 3.12 (see Entry 1 and Entry 9). A 3.14 venv installs cleanly and then fails at import time with what look like three unrelated corrupt-package errors.

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

### 2026-07-21 — Entry 6: V1.5 complete — safety substrate, Resolution, approvals, Communication, memory, V1.5 frontend
- **V1.5a** (FR-4.2/FR-5.3/FR-12): migration `0003` with every V1.5 table; `safety/{resource_lease,circuit_breaker,kill_switch}`. Load-bearing invariants live in Postgres, not application code — the `resource_leases` partial unique index makes a 4-way concurrent race produce exactly one winner (tested). New `system_flags` table added to ESD §6.
- **V1.5b** (FR-4, FR-8.3): separate write ServiceAccount (`delete pods` + `deployments/scale` only — the `/scale` subresource *is* k8s's replicas-only patch scope); `restart_pod`/`scale_deployment` MCP tools with one-attempt (never blind-retried) writes; action catalog where a forward action cannot be defined without its compensating action; four ordered execution gates (kill switch → expiry → tier/approval → observer+blast-radius+breaker) with lease-wrapped execution. **Tier-1 shadow mode defaults ON.** Compensating action proven to reverse real state, not just to have been called.
- **V1.5c** (FR-5/6/7): approvals with server-side RBAC (approve in API, execute in worker — ESD §11); plain-English Communication Agent with a unit test asserting no jargon token can reach a stakeholder; memory on Postgres instead of Mem0 (same interface, one fewer dependency) with a human approval gate before anything becomes retrievable; slack + pagerduty_mock MCP servers. **State-machine change:** `remediation_proposed → resolved` added so a rejected proposal (FR-5.2) doesn't strand the incident.
- **V1.5d**: monochrome two-theme design system (pure black / pure white, CSS-variable tokens, no brand hue anywhere — only the severity ramp and ok/danger carry colour, because P1 and P4 must not render identically); pre-paint theme script; Apple-style public marketing homepage at `/` with a CSS-drawn product still; approvals + safety pages. ESD §5 updated with the documented shadcn deviation.
- **Convention introduced:** UI colour is a two-theme token set in `globals.css`; components never hardcode a hex or a Tailwind palette colour, and any new chromatic value needs a semantic justification (it must carry information greyscale cannot).
- **Verified live:** Tier-2 scale proposal → API approval as on-call → worker executed it against the real kind cluster (checkout-service 2→3 replicas) → incident reached `monitoring` with a plain-English update → compensating action restored 3→2 and marked `rolled_back` → resolve drafted a memory summary held pending approval. Writer RBAC negative-checked (`delete pods` yes, `get secrets` no).

### 2026-07-21 — Entry 7: Real LLM integration + removal of every stub/fake path
- **Real models only.** `providers/openrouter.py` (OpenAI-compatible, free-tier open models) with **key rotation** and a **model fallback chain**, because throttling is the normal case on free tiers. Real token/cost accounting from the API `usage` block (FR-8.2, ESD §15). Models chosen by measurement, not assumption: `nemotron-3-super-120b` for RCA, `nemotron-nano-9b` for the cheaper agents.
- **Every fake path deleted from the product:** `providers/stub.py`, `llm_degrade_to_stub`, the factory's stub branch, `FixtureGateway` (moved to `tests/support/doubles.py`), the **PagerDuty-mock MCP server + fixtures**, and the homepage's mocked-up incident. The factory now accepts only `openrouter`; exhaustion raises `ProviderExhausted` and the incident waits for the retry sweep.
- **Convention introduced (Section 18):** *degrade throughput, never truthfulness.* Aegis ships no stub/mock/offline LLM or evidence path. Anything that could answer without a real model is a defect, because its output is indistinguishable from a real analysis exactly where a human decides to trust it. Test doubles live under `tests/support/` and are unreachable from runtime. Guarded by `tests/unit/test_no_fabrication.py`.
- **Grounding hardened after real-model failures:** citation-resolves is necessary but **not sufficient** — `check_category_support` requires the cited evidence to actually support the asserted category (caught the real case of "a recent deployment caused this" with the deploy source down and zero deploy evidence). ESD §25 gained two trade-off rows. A degraded ensemble (fewer passes than requested) is always flagged low-confidence instead of reporting a lone pass as `agreement 1.00`.
- **Assumption surfaced:** free-tier upstream capacity is intermittent. The eval harness now fails loudly when throttled rather than printing numbers that did not come from a real model; re-run `eval/run_real_eval.py` when capacity returns.
- Tests: 166 passing (new `test_grounding.py` ×14, `test_no_fabrication.py` ×9).

### 2026-07-21 — Entry 8: Firebase/Google auth, real Redis, health + metrics (V2a)
- **Scope correction first.** The directive was to remove every mock/stub/placeholder. Audited
  before building: Entry 7 had already removed them, and `tests/unit/test_no_fabrication.py`
  fails the build if a stub provider reappears. The remaining `mock`/`fake` hits were legitimate
  `httpx.MockTransport` contract fixtures under `tests/` and stale prose in docs. The genuine
  gaps were: no Firebase auth, **Redis declared everywhere and used nowhere**, a hashing
  (non-semantic) embedder, no LLM streaming, and stale docs. Built against that list, not the
  assumed one.
- **Firebase Authentication + Google OAuth (ESD §8).** Architecture review written first per §2
  (new external dependency + new auth surface; findings in BUILD_LOG V2a). Token-exchange design:
  the client SDK completes the Google popup only, its ID token is POSTed once to
  `POST /auth/session`, verified server-side by the Admin SDK (signature, issuer, audience,
  expiry, revocation, `email_verified`), and discarded — client persistence is set to
  **in-memory before sign-in** so it never reaches IndexedDB, and `signOut()` follows the
  exchange. **CLAUDE.md §12 is preserved, not amended.** Password auth is gone entirely:
  `/auth/login`, `hash_password`/`verify_password`, the `passlib`/`bcrypt` dependency, and
  `db/seed.py` were all deleted rather than left dormant.
- **Authorization fails closed.** `AEGIS_ADMIN_EMAILS` / `AEGIS_APPROVER_EMAILS` grant elevated
  roles; every other authenticated Google account is provisioned `viewer`, which cannot approve a
  remediation. The role is re-resolved on **every** sign-in, so revoking an approver is an env
  change plus their next login rather than a manual DB edit. This allowlist is the only thing
  standing between "any Google account can sign in" and cluster write access — tested explicitly.
- **Schema (migration `0004`).** `users.hashed_password` becomes **nullable** rather than being
  filled with a sentinel: a federated user genuinely has no password, and a sentinel would be
  indistinguishable from a real hash to every reader of the column (§15). Added `firebase_uid`
  (unique, nullable — matched before email so an email change doesn't orphan an account),
  `display_name`, `photo_url`, `last_login_at`.
- **Redis is now real (ESD §14).** Previously present in `docker-compose.yml`, `pyproject.toml`
  and `config.py` with **zero call sites**. Now backs a 30s read-only MCP evidence cache and HTTP
  rate limiting. Verified live: real keys, counters and TTLs in the container.
- **New convention — every Redis path fails open.** Redis is non-authoritative (§5), so a cache
  miss does the real work and a rate-limit check allows the request when Redis is down. Failing
  closed would convert a cache outage into a total outage. This is only safe because the
  load-bearing limits (Tier-1 rate limit, circuit breaker, leases, kill switch) are enforced in
  Postgres, and they deliberately stay there.
- **New convention — the evidence cache is an allowlist of read tools, never a denylist of
  writes.** With a denylist, a write tool added later is cacheable until someone remembers to
  exclude it, and the failure mode is a cached success for a `restart_pod` that never executed.
  Guarded by a structural test asserting no write verb can enter the allowlist.
- **Bug found and fixed while verifying.** The rate limiter returned **500 instead of 429**:
  `@app.middleware("http")` runs *outside* FastAPI's exception-handler stack, so a raised
  `AegisError` escapes unhandled. The pre-existing `webhook_guard` had the same latent defect and
  would have returned 500 instead of 401 for a bad webhook token. Both now return
  `errors.error_response(...)`; `tests/integration/test_middleware.py` guards the regression.
- **Health and metrics (ESD §7).** `/health` distinguishes dependencies by role — Postgres
  unreachable → 503 (pull the instance), Redis unreachable → 200 `degraded` (never take an
  instance out of rotation over a cache). `/metrics` is authenticated and reports incidents by
  state, cumulative LLM tokens/cost/latency, and Redis keyspace stats.
- **Verified live, not just in tests:** Firebase Admin initialized against the real project and
  rejected a forged token (`InvalidIdTokenError`, token never logged); `/auth/login` now 404s;
  120 requests allowed then a real 429 with `Retry-After`; Redis stopped → `degraded` at 200 with
  the API still serving; Redis restarted → `ok`. **188 tests passing**, ruff clean, frontend
  build/lint/typecheck clean.
- **Still open (not done, deliberately):** the runbook embedder is still the non-semantic
  `HashingEmbedder` (BGE swap agreed), and there is still no LLM streaming. Neither is claimed as
  complete anywhere.
- **Constraint surfaced:** `redis.asyncio` binds connections to the creating event loop, so the
  client is cached **per loop**, not per process — a process-global client silently breaks any
  runtime that uses more than one loop.
- **Assumption surfaced (since resolved — see Entry 9):** the Firebase credentials supplied for
  this entry belonged to a non-Aegis project, so Aegis shared a user pool with another
  application and the allowlist was the only thing making that acceptable. A dedicated project
  was recorded as the correct end state; Entry 9 migrated to one.

### 2026-07-21 — Entry 9: Dedicated Firebase project, semantic embeddings, production RAG (V2b)
- **Firebase migrated to the dedicated `aegis-ai-detective` project.** This closes the Entry 8
  assumption: Aegis no longer shares a user pool with an unrelated application. Full replacement,
  not an incremental edit — service-account JSON, `FIREBASE_PROJECT_ID`, and all six
  `NEXT_PUBLIC_FIREBASE_*` values. A repo-wide sweep confirms no old project id, API key, app id,
  sender id, bucket, or auth domain survives outside two historical journal notes, which were
  amended (not erased — these logs are append-only, §19) because their claims had become false.
  **No credential is hardcoded anywhere**: every value lives in gitignored env/secret files, which
  is why the migration touched no source file.
- **Environment defect found and fixed.** The backend venv had been recreated on **Python 3.14**
  at the old path while the installed packages were `cp312` builds, so `pydantic_core`,
  `asyncpg.protocol`, and `xxhash` all failed to import. CLAUDE.md pins 3.12 for exactly this
  reason (Entry 1). Rebuilt on 3.12.10; suite green again. Worth knowing: the symptom presented as
  three unrelated "corrupt package" errors, not as a version mismatch.
- **Real semantic embeddings (FR-3.3, ESD §20).** `HashingEmbedder` is **deleted**. Replaced with
  local ONNX **BGE (`bge-small-en-v1.5`) via `fastembed`** — chosen over sentence-transformers
  because it is the same model quality for ~90MB of dependencies rather than ~2.5GB of torch, and
  over a hosted API because retrieval must not acquire a network hop or a vendor (§18). Async
  thread-offload so CPU inference never blocks the event loop; configurable model/dimension/batch
  size, with a **load-time guard** that fails loudly if the model's width disagrees with
  `EMBEDDING_DIM` (the alternative is an opaque pgvector error at INSERT).
  BGE's query/passage asymmetry is honoured — queries go through `query_embed` for the
  instruction prefix, passages do not; using one for both measurably degrades retrieval.
- **Production RAG.** Retrieval now targets **chunks**, not documents (`runbook_chunks`,
  migration `0005`): structure-aware Markdown chunking with configurable size/overlap, heading
  trails carried into the embedded text, **hybrid** retrieval (pgvector HNSW + Postgres full-text)
  fused with **Reciprocal Rank Fusion**, **metadata filtering** on `service_tags` applied inside
  both retrievers, **cross-encoder reranking** over an over-fetched candidate pool, configurable
  top-k, and section-level citations. Every stage degrades independently — no reranker → fused
  order, no lexical hits → semantic order, no embedder → lexical only.
- **New convention — RRF over score blending for hybrid retrieval.** A cosine distance and a
  `ts_rank` are not commensurable, so any fixed weighting is arbitrary and drifts as the corpus
  changes. Fuse *ranks*, which are comparable by construction.
- **New convention — index freshness is content AND model AND dimension AND chunks-exist.**
  Introduced because hash-only freshness was a **real shipped defect**: right after chunking
  landed, re-ingestion reported `changed=false` for every document and built **zero chunks**, so
  every RAG query would have returned nothing while ingestion logged success. `runbooks` now
  records `embedding_model`/`embedding_dim` (migration `0006`), so changing the model re-indexes
  automatically. Both failure modes have regression tests.
- **Bug fixed: chunking silently dropped short sections.** `min_chars` was applied to whole
  sections, so a terse "Mitigation: raise the memory limit and redeploy" — the most actionable
  content in the corpus — was discarded. It now applies only to fragments produced by splitting.
- **Bug fixed: citations stuttered** as "Runbook: OOM › Runbook: OOM", because a document's H1 is
  usually also its title. Regression-tested.
- **Verified live, not just in tests:** Firebase Admin initializes against `aegis-ai-detective`
  and rejects a forged token; `/auth/login` still 404s; the real corpus re-indexed and answers
  "containers keep dying because they used too much RAM" with the OOM runbook top-1 **despite zero
  shared vocabulary** — the query the old embedder structurally could not serve — while the exact
  identifier `OOMKilled` is matched by `semantic,lexical` together, and a `catalog-service` filter
  returns only chunks tagged for it. **211 tests passing** (was 188), ruff clean.
- **Constraint surfaced:** the shipped runbook corpus is four ~650-char documents with no
  subheadings, so it yields one chunk each. Chunking is correct but its value is unexercised at
  this corpus size; the multi-section behaviour is covered by unit and integration tests using
  structured fixtures.
- **Still open (not done, not claimed):** LLM streaming, and the deeper observability pass
  (trace IDs, per-stage latency for embedding/retrieval/MCP).

### 2026-07-21 — Entry 10: AI-layer audit + real LLM reasoning, guardrails, versioned prompts (V2c)
- **Audit first, and it contradicted the project's own claims.** Of seven "agents", exactly
  **one** (RCA) called an LLM. Triage was two `set` lookups; Correlation was five hardcoded MCP
  calls in fixed order; Observer was regexes; Communication was `str.format()`; Resolution was a
  dict lookup; the "Supervisor" graph was static edges with one boolean conditional.
  `langchain-core` and `langsmith` were installed and **imported nowhere**. Recorded in full in
  BUILD_LOG V2c.
- **Safety carve-out, agreed explicitly before building.** The literal instruction was that every
  decision become an LLM call. Five components stay deterministic because converting them is a
  security regression, not an upgrade: Observer citation-resolution (a watchdog must not share
  the failure modes of the thing it watches), redaction (an LLM redactor can be injected into not
  redacting), the four execution gates (an LLM deciding "is this permitted" means an injected log
  line can authorise a cluster write), the ingestion severity floor (PRD 11A requires severity in
  <1s), and state-machine transitions. This matches the directive's own closing rule that Python
  owns *security and deterministic business logic*.
- **New convention — LLM judgment is clamped, never trusted, where safety depends on it.** Triage
  now genuinely reasons about customer impact, but may only **raise** severity above the
  rule-based floor, never lower it. Escalation is judgment; de-escalation is an attack surface —
  otherwise a crafted alert title could downgrade a P1 and suppress the page. Tested both ways.
- **New convention — guardrails and the Observer are different layers.** The Observer validates
  *reasoning*, once, at the end. Guardrails (`guardrails/`) govern *every model interaction*,
  including calls no Observer ever sees. Guardrails are deterministic on purpose: a model asked
  to detect prompt injection is itself an injection target.
- **New convention — ingress fails open, egress fails closed.** A jailbreak pattern in evidence is
  recorded and passed through (blocking would let anyone who can write a log line deny incident
  response); credential-shaped model output is blocked outright (unrecoverable once sent).
- **Versioned prompts (`agents/prompts/`).** Every prompt has an id, version, declared input
  contract, and content fingerprint, recorded on each agent step as `prompt_ref`. Rendering with a
  missing variable now raises instead of sending a literal `{service}` to a model — which would
  produce a plausible answer to a malformed question, the one failure nothing downstream detects.
- **Structured outputs are enforced, not hoped for.** `complete_structured()` requests the schema
  from the API, validates with Pydantic, and repairs by feeding the *validation error* back
  (a blind retry re-samples the same misunderstanding). Unrepairable output raises rather than
  returning a half-parsed object. Also added real token streaming, which deliberately does **not**
  fail over between models after the first delta — that would splice two completions into one
  apparently coherent answer.
- **Bug found by the audit: the alert kind was discarded at ingestion.** `AlertIn.kind` was used
  for the provisional severity and then thrown away, so the graph's triage node called
  `classify_severity(service, "error_rate")` with the kind **hardcoded** — every incident was
  triaged as an error-rate alert whatever had actually fired. Migration `0007` persists
  `alert_kind`/`alert_value`; existing rows are left NULL rather than backfilled, since inventing
  "error_rate" would bake the bug into history.
- **Bug fixed: `CritiqueResult.verdict` was a bare `str`** with its allowed values stated only in
  prose, so structured-output enforcement could not constrain it and "approve with reservations"
  would have read as a rejection. Now a `Literal`.
- **Verified:** 244 tests passing (was 211), ruff clean. Live verification of the *LLM output* is
  **blocked on OpenRouter daily quota**, not on code — but the quota exhaustion did verify the
  degradation paths for real: Triage fell back to the rule floor and Communication to its
  template, and the pipeline continued rather than stalling.
- **Still open (not claimed):** Correlation tool-selection loop and supervisor routing are
  designed and prompted (`correlation.plan`, `supervisor.route` are registered) but not yet
  wired — Correlation still calls its fixed five-tool sequence. LangSmith tracing and
  DeepEval/RAGAS evaluation are not started. LangChain remains unused.

### 2026-07-21 — Entry 11: Agentic Correlation + LLM supervisor; every call site on the registry (V2d)
- **Correlation is now an LLM tool-calling loop** (`agents/correlation/planner.py`). Replaces a
  fixed five-call sequence that ran identically for every incident and could not follow a lead —
  it never fetched a *specific* pod's detail because it did not know which pod was unhealthy
  until after it had finished. The loop is plan → dispatch → observe → re-plan, so round two acts
  on what round one found.
- **New convention — the model chooses tools from an allowlist; Python dispatches them.**
  `agents/correlation/tools.py` is a read-only catalog. A tool name absent from it is never
  dispatched, so evidence gathering is structurally incapable of reaching `restart_pod` or
  `scale_deployment` no matter what an injected log line requests. Rejections are returned to the
  model with a reason rather than dropped, because a silently dropped call looks to the model
  like a tool that returned nothing — and it will reason from that absence.
- **Supervisor routing is now an LLM decision** (`orchestrator/supervisor.py`), and the graph is a
  real cycle: every step returns to the supervisor. `available_steps()` computes the legal set
  from state and a choice outside it is overridden — so the model can decide another RCA pass is
  needed but cannot exceed the revision limit, exceed the token budget, reach `resolution`
  without an observer-validated hypothesis, or decline to ever finalize.
- **New convention — an agentic loop needs a cap per *step type*, not only a global one.** Two
  real loops were found and fixed while building this: (1) gating correlation on *evidence
  gathered* rather than *having run* looped forever when every source was down — correlation was
  the only legal step, produced nothing but gaps, and was chosen again; (2) `correlation` stayed
  permanently available after a rejected hypothesis, and "gather more evidence" is always a
  plausible next step, so a model that favours it never concludes. Both hit LangGraph's recursion
  limit rather than terminating. Now bounded by `correlation_max_invocations`, with the recursion
  limit retained as a backstop for the case where routing and bounds disagree.
- **Graph flow bug fixed:** the supervisor could route to `resolution`, which had a direct edge to
  `END` — so an incident could finish without `finalize`, meaning no state transition and no audit
  record. `resolution` now returns to the supervisor; only `finalize`/`escalate` end the run.
- **Every LLM call site is now compliant.** The audit found RCA — the one agent that had always
  been "real AI" — was the last using an inline f-string, raw `complete()`, and no guardrails.
  It now renders `rca.hypothesis` from the registry, records `prompt_ref`, and screens its prompt.
  Guarded by `tests/unit/test_llm_call_compliance.py`, which parses the AST of every module and
  fails on a call site that skips the registry, `prompt_ref`, or guardrails — **and** fails if one
  of the six agent modules stops calling a model at all, which is how fake AI would creep back.
- **Verified:** 267 tests passing (was 244), ruff clean.
- **Still open, explicitly not done:** LangSmith tracing (item 4) and the DeepEval/RAGAS
  evaluation framework (item 5) are **not started**. `langsmith` and `langchain-core` remain
  installed and unused. RCA still uses `complete()` with per-pass parsing rather than
  `complete_structured()` — deliberate, because the ensemble needs pass-level retry semantics
  that `_parse_pass` provides, but it means RCA alone does not get schema enforcement at the API
  layer. End-to-end validation against real models (item 7) remains blocked on OpenRouter quota.

### 2026-07-21 — Entry 12: LangSmith tracing + evaluation framework (V2e)
- **LangSmith (`core/tracing.py`).** Traces every graph node, every LLM call (model actually used
  *after fallback*, tokens, cost, latency, schema-repair attempts) and every MCP tool call, with
  one trace per investigation tagged by `incident_id` so a production incident is findable by the
  same id used everywhere else. Wrapping happens at the **provider**, not per agent, because that
  is the only place that knows which model actually answered — precisely the field needed when
  diagnosing why one incident behaved differently from another.
- **New convention — observability is never load-bearing.** Every tracing entry point is a no-op
  when LangSmith is unconfigured or unreachable, and `traced()` returns the *undecorated*
  function when tracing is off, so a traced and untraced deployment are behaviourally identical.
  A tool that explains failures must not be able to cause one.
- **Evaluation framework (`evaluation/`), split by what each metric costs to run.** Deterministic
  retrieval metrics (hit rate, MRR, precision@k, NDCG@k, forbidden rate) are ~40 lines of
  arithmetic — no model, no network, no quota — so they gate **every commit**. LLM-judged RAGAS
  metrics (faithfulness, relevancy, context precision) cost tokens, so they are on-demand.
- **New convention — evaluation dependencies are not runtime dependencies.** RAGAS is an optional
  extra (`pip install -e ".[eval]"`). It pulls ~37 packages including pandas, pyarrow, datasets,
  langchain and openai, and downgrades `fsspec`, which the embedding stack depends on. Shipping
  that into the production image to support a CI-only measurement is a poor trade; absence
  degrades to `available=False`, never an import error.
- **New convention — a golden case must not reuse the corpus's own vocabulary.** A query phrased
  in the document's words measures string matching, not retrieval. The dataset deliberately
  includes adversarial cases: wrong-service queries, symptom-only queries with zero lexical
  overlap, and an **unanswerable** query where returning nothing is the correct behaviour.
- **The framework immediately found real weaknesses, which is the point.** Measured against the
  actual shipped corpus: `hit_rate=0.86 mrr=0.65 ndcg=0.70 forbidden=0.14`. The `unanswerable`
  case **fails**: `rag_min_score` defaults to `0.0`, so there is **no relevance floor** and
  retrieval always returns `k` results however irrelevant — an out-of-domain query returns
  confident operational runbooks, feeding distractors straight into the RCA prompt. `mrr=0.65`
  shows two cases finding the right document only at rank 3–4, which hit rate alone hides.
  Recorded as the honest baseline, not a target.
- **Verified:** 274 tests passing (was 267), ruff clean.
- **Still open:** the relevance floor above; LangSmith is inert until `LANGSMITH_API_KEY` is set;
  RAGAS metrics are wired but have never been executed (they need the extra *and* a judge model),
  so **no faithfulness or hallucination number exists yet**; end-to-end real-model validation
  remains blocked on OpenRouter quota.

### 2026-07-21 — Entry 13: Environment repair, second LLM vendor, untested security control (V2f)
- **Audit first, and the repo's own status line was wrong.** CLAUDE.md claimed "274 backend
  tests pass". A clean run gave **5 failed, 215 passed, 54 skipped**. Two independent causes,
  both invisible to anyone reading the log: `fastembed` was declared in `pyproject.toml` but
  **not installed** in the venv, so the entire embedding/RAG layer failed at import; and the 54
  integration tests were **silently skipping** because Postgres/Redis were not running, so they
  had never actually executed. A suite that skips on missing infrastructure reports green while
  proving nothing — the skip count is the number worth reading, not the pass count.
- **`.env` was a byte-identical copy of `.env.example`.** Placeholder OpenRouter keys, placeholder
  Firebase project id, placeholder `JWT_SECRET`, and `K8S_API_URL` pointing at `6443` while kind
  actually maps a random host port. Nothing touching an external service could have worked, despite
  Entries 8–12 describing live verification. Populated from the operator's existing credential
  files; **no source file changed**, which is the point of keeping every value in env.
- **New convention — env files are written as UTF-8 explicitly.** Repairing `.env` on Windows
  wrote `§` as cp1252 (`0xa7`), and `python-dotenv` reads UTF-8, so the whole file failed to parse
  with a `UnicodeDecodeError` pointing at a byte offset rather than at the setting. Any tooling
  that rewrites an env file must encode explicitly rather than relying on the platform default.
- **Second LLM vendor (`providers/gemini.py`, ESD §20).** OpenRouter's free models share one
  account-wide *daily* cap; when it is spent no agent can reason until the UTC reset, which is not
  a throttle the retry sweep can ride out, and it had blocked end-to-end AI validation for two
  entries. All four keys were exhausted at audit time. Gemini is an independent capacity pool
  behind the same `LLMProvider` Strategy, with the same two recovery axes (key rotation, model
  fallback) because free capacity is unreliable by default — `gemini-3.5-flash` answering 503
  "high demand" while `gemini-3.1-flash-lite` served normally was the *observed* state, not a
  hypothetical.
- **New convention — providers are alternatives, not an automatic chain.** Falling back across
  *vendors* would make it unclear after the fact which model produced a given hypothesis, and that
  attribution is what makes a quality regression diagnosable. Switching vendor is a config change.
- **Shared enforcement, hoisted rather than copied.** `providers/errors.py` (the failure taxonomy,
  previously defined inside the OpenRouter module — so "handle exhaustion" implicitly meant
  "import OpenRouter", the wrong dependency direction for a Strategy) and `providers/structured.py`
  (the schema validate-and-repair loop). A second provider must not be able to ship a weaker
  version of a guarantee callers branch on.
- **`providers/gemini_schema.py`: Gemini does not accept JSON Schema.** It accepts an OpenAPI
  subset — no `$ref`/`$defs`, uppercase type names, no `title`/`default`/`additionalProperties`,
  and optionals as `nullable` rather than `anyOf: [T, null]`. This is separately and directly
  tested because its failure mode is *silent*: a degraded schema yields output that fails Pydantic
  validation downstream, and the repair loop misreads that as the model's fault and burns the whole
  repair budget re-asking a question that was malformed before it was sent. `propertyOrdering` is
  emitted because Gemini's decoder is order-sensitive.
- **Bug found and fixed while writing tests: a test that could not fail.** The credential-hygiene
  test used `caplog`, but the project configures structlog with a `PrintLoggerFactory` that writes
  to stdout and never reaches stdlib logging — so it captured nothing and passed vacuously. Now
  uses `capsys` and asserts the log output is *non-empty* before asserting the key is absent, so it
  fails loudly if logging is rewired again rather than silently going vacuous.
- **Security control that was never tested, and only passed while disabled.** The ingest webhook
  guard had no coverage in `test_ingestion_api.py`; the tests inherited whatever
  `INGEST_WEBHOOK_TOKEN` the developer happened to have locally, so setting a real token turned
  every ingestion test into a 401. The fixture now pins the token and sends it by default — every
  ingestion test exercises the guarded path — plus three new rejection tests, including one proving
  a rejected request performs **no write** (a guard that 401s after inserting is cosmetic).
- **Verified live, not just in tests:** `gemini-3.1-flash-lite` answered `complete` (1.0s),
  `complete_structured` (valid on the **first** attempt, 0 repairs — the schema translation is
  correct end-to-end, including `Literal` enums, bounded floats, nested lists and optionals), and
  `stream`. **303 tests pass, 0 failed, 0 skipped** (was 215 passed / 5 failed / 54 skipped); ruff
  clean.
- **Still open, explicitly not claimed:** the RAG relevance floor (`rag_min_score=0.0`) is
  unchanged and the `unanswerable` golden case still fails; LangSmith remains inert without
  `LANGSMITH_API_KEY`; RAGAS has still never been executed; Resolution and Memory remain
  deterministic; the frontend has not been touched this entry and still lacks search, filters,
  profile, notifications and general settings; `/health` and `/metrics` live at `/api/v1/*`, not
  the paths ESD §7 documents; and `/docs`, `/redoc`, `/openapi.json` are publicly exposed.
