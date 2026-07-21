# BUILD_LOG.md — Aegis Development Journal

The detailed, running record of how Aegis is built, milestone by milestone. This is the companion
to CLAUDE.md's Feature Log (which keeps brief pointers): here we capture *what* was built, *which*
PRD FR/NFR and ESD sections it satisfies, the *decisions* made, the *tests* added, and *how to run*
each piece. Source-of-truth hierarchy: PRD.md → ESD.md → CLAUDE.md → this log.

---

## Current status / resume here (as of 2026-07-21)

**MVP, V1.5, V2a (auth + Redis), V2b (semantic RAG), V2c/V2d (real LLM agents) and V2e
(tracing + evaluation) are complete.** **274 backend tests** pass; ruff clean; frontend
`next build` / `next lint` / `tsc --noEmit` clean.

Six of seven agents now perform genuine LLM reasoning; the seventh (Resolution) is a deliberately
deterministic action catalog behind the four safety gates. Routing is an LLM supervisor over a
real graph cycle with Python-enforced bounds. Every LLM call site uses the versioned prompt
registry, guardrails and `prompt_ref`, enforced structurally in CI by
`tests/unit/test_llm_call_compliance.py`.

**Open follow-ups, in priority order:**
1. **`rag_min_score` is `0.0`, so retrieval has no relevance floor.** An out-of-domain query
   returns confident operational runbooks (measured — see the V2e eval results below). Fixing it
   needs care: cross-encoder scores are unbounded logits, not probabilities.
2. **LangSmith is wired but inert** — set `LANGSMITH_API_KEY` to get traces.
3. **LLM output quality is unverified end-to-end**, blocked on OpenRouter daily quota. Degradation
   paths were verified live under real quota exhaustion.
4. **RAGAS metrics are wired but never executed** — they need both the optional extra and a judge
   model. No faithfulness/hallucination number exists yet.
5. **Resolution and Memory remain deterministic** (category→action catalog; SQL recall filter).
   Both are LLM candidates; the safety gates around Resolution are not.
6. A LangGraph Postgres checkpointer (deferred — see the ESD §25 row), rolling `audit_log`
   partition maintenance, and the public demo deployment (ESD §18).

**Environment quick-start:** `docker compose up -d` (Postgres :5433) · kind cluster `aegis`
(`bash infra/setup-cluster.sh` if gone) · `bash infra/gen-mcp-credentials.sh` **each session**
(tokens live 24h; prints the current `K8S_API_URL` — kind's port changes if the cluster is
rebuilt) · API `uvicorn api.main:app --port 8000` · worker `python -m worker.main` · frontend
`npm run dev` · seed `python -m rag.seed`. There is no user seed script: auth is federated, so
an Aegis user row is created on that person's first Google sign-in, with the role their email
resolves to in `AEGIS_ADMIN_EMAILS` / `AEGIS_APPROVER_EMAILS`. All three processes are also
defined in `.claude/launch.json`.

**Gotcha:** never run `next build` while the dev server is up — it corrupts `.next` and the app
serves unstyled HTML until you stop the server, `rm -rf .next`, and restart.

---

## Milestone 0 — Rename to Aegis + repo scaffold

**Status:** complete.

### What changed
- **Rename:** every occurrence of *IncidentPilot / incidentpilot* → *Aegis / aegis* across
  `docs/PRD.md`, `docs/ESD.md`, and `CLAUDE.md` (10 occurrences). FR/NFR numbers untouched. The
  target GitHub repository is now `aegis` (the earlier `.../incidentpilot` URL is superseded, per the
  user's decision).
- **Scaffold:** created the full project tree from ESD §21 —
  `backend/`, `frontend/`, `infra/`, `eval/`, `docs/`. Backend modules live directly under
  `backend/` (`agents/{triage,correlation,rca,observer}`, `api`, `core`, `db`, `mcp_servers/...`,
  `orchestrator`, `providers`, `rag`, `redaction`, `worker`, `tests/{unit,contract,eval,fault_injection}`).
- **Config files:** `backend/pyproject.toml` (deps + ruff + pytest), root `docker-compose.yml`
  (Postgres/pgvector + Redis), `.gitignore`, `.env.example` (no secrets), root `README.md`, this log.
- Moved `PRD.md`/`ESD.md` into `docs/`; each backend package got an `__init__.py` docstring naming
  its pipeline role (CLAUDE.md §6).

### Decisions
- **Deviation from ESD §21 (documented):** `CLAUDE.md` stays at the **repo root**, not under `docs/`,
  because the Claude Code / agent tooling loads it from the project root. PRD.md and ESD.md did move
  into `docs/` as specified. Reasoned, deliberate deviation per CLAUDE.md §6.
- **Two new top-level backend modules** beyond ESD §21's original list: `providers/` (the LLM
  provider Strategy abstraction, ESD §20) and `core/` (settings, structured logging, DB engine —
  cross-cutting infra, ESD §13). ESD §21 updated in the same milestone to list both (CLAUDE.md §9).
- **Python version:** `requires-python >=3.12`. The host has 3.14; LangGraph/pydantic-core wheels may
  lag 3.14, so the backend venv should be created on **Python 3.12** if any wheel fails to build.
- **LLM default = stub** (`LLM_PROVIDER=stub`): the whole system runs and tests pass with zero API
  keys; real providers plug in later behind `providers/` (ESD §20).

### Tests
- None yet (scaffold only). First tests land in Milestone 1 (state-machine transition validation).

### How to run
See `README.md` → *Local development*. Nothing executable ships in this milestone beyond the
datastores (`docker compose up -d`).

### Follow-ups / open items
- `kind`, `kubectl`, `helm`, and `gh` are **not installed** on the host yet — needed in M2 (cluster)
  and M8 (push). Will install/handle when those milestones begin.

---

## Milestone 1 — Data layer (Postgres + pgvector)

**Status:** complete.

### What changed
- **Environment:** installed **Python 3.12.10** (via `py install 3.12`) and created `backend/.venv`
  on it; `pip install -e ".[dev]"` succeeded (langgraph 1.2.9, sqlalchemy 2.0.51, pydantic 2.13,
  asyncpg 0.31, pgvector 0.5, etc.). The 3.14 wheel-lag risk from M0 is resolved by pinning 3.12.
- **Core infra:** `core/config.py` (typed `Settings` from env/.env, the single config read point),
  `core/logging.py` (structlog JSON, `incident_id`-bound loggers — CLAUDE.md §17),
  `core/db.py` (async engine + `session_scope` transactional context).
- **Enums** (`db/enums.py`): `StrEnum`s for severity (P1-P4), incident state, actor/alert/evidence/
  message types, user role, runbook source. `name == value` so SQLAlchemy and the native Postgres
  enum agree on the persisted label.
- **State machine** (`db/state_machine.py`): pure-logic allow-list of legal `(from,to)` incident
  transitions (ESD §6.1) with `assert_legal_transition`; no I/O, so it is unit-tested in isolation.
- **ORM models** (`db/models/`): incidents (+`UNIQUE(alert_source, external_alert_id)` for FR-10),
  incident_state_transitions, agent_steps, agent_messages, evidence_citations, runbooks (pgvector
  `Vector(768)` + `content_hash`), users, audit_log (composite `(id, created_at)` PK for partitioning).
- **Repository layer** (`db/repository.py`, ESD §24): `IncidentRepository` with the idempotent
  `upsert_incident` (INSERT … ON CONFLICT DO NOTHING → returns existing, never duplicates — FR-10.1),
  `record_transition` (validates via the state machine before writing), plus `AgentRepository` and
  `AuditRepository`.
- **Migrations** (`alembic.ini`, `db/migrations/`): hand-written `0001_initial` — enables `vector`,
  creates native enum types, all MVP tables, the runbooks **HNSW** index (cosine), and `audit_log`
  as a **range-partitioned** table with 13 monthly child partitions (ESD §17).

### Decisions
- **Native Postgres enums** used instead of a `String` + `CHECK` constraint (ESD §6.1 wording).
  Both limit `incidents.state` to the defined set; native enums are stronger and idiomatic. All
  states (incl. V1.5 remediation_*) are defined now so no enum migration is needed at V1.5.
- **audit_log partitions** are pre-created for a fixed 13-month window starting 2026-07 (no DEFAULT
  partition, to keep future partition attachment clean). Rolling partition maintenance is a
  documented ops task, not built in MVP.
- **Line length** standardized at 100 (ruff `E,F,I,B,C4,UP`). `StrEnum` adopted (UP042).

### Tests
- `tests/unit/test_state_machine.py` (transition allow-list: legal accepted, illegal rejected, V1.5
  chain present, completeness) and `tests/unit/test_enums.py` (pinned enum values). **27 passing**,
  `ruff check` + `ruff format --check` clean.
- Live-DB verification: `docker compose up` + `alembic upgrade head` applies the full schema
  (tables, 13 audit_log partitions, HNSW index) against real Postgres+pgvector.

### How to run
```bash
docker compose up -d
cd backend && ./.venv/Scripts/python.exe -m alembic upgrade head
./.venv/Scripts/python.exe -m pytest tests/unit -q
```

### Follow-ups / open items
- Idempotency of `upsert_incident` gets its behavioral (DB-backed) test in M5 alongside the
  ingestion API, per the plan.
- **Host port:** the host already runs a native PostgreSQL 13 on `0.0.0.0:5432`, so the container
  publishes Postgres on host **5433** (`5433:5432`) and `.env` uses `127.0.0.1:5433`. Documented in
  `docker-compose.yml`. Verified: `alembic upgrade head` created all 8 base tables + 13 `audit_log`
  monthly partitions + the runbooks HNSW cosine index + the FR-10 unique constraint.

---

## Milestone 2 — kind cluster + Meridian Commerce + Prometheus

**Status:** complete.

### What changed
- **Tooling:** installed `kind` v0.32.0 and `helm` v4.2.3 into `~/bin`; reused Docker Desktop's
  `kubectl` v1.34.1. Scripts take these via `KIND`/`HELM`/`KUBECTL` env vars (no PATH assumptions).
- **Cluster** (`infra/kind/cluster.yaml`): single-node `aegis` cluster; host `:9090`→Prometheus
  NodePort 30090 and host `:9093`→Alertmanager NodePort 30093, so the host-run Prometheus MCP server
  (M3) and the Alertmanager alert source reach the cluster directly.
- **Meridian Commerce** (`infra/meridian/`): one FastAPI image parameterised by `SERVICE_NAME`,
  backing `checkout-service` / `payment-service` / `catalog-service`. A background loop simulates
  traffic and applies the active failure mode, exposing `http_requests_total`,
  `http_request_duration_seconds`, `app_up`, `app_injected_error_rate`, plus `POST /admin/failure`
  to inject error-rate / latency faults (ESD §18).
- **kube-prometheus-stack** (`infra/kube-prometheus-stack/values.yaml`): installed via Helm; Grafana
  disabled, short retention, modest resource limits for a laptop; `serviceMonitorSelectorNilUses
  HelmValues: false` so the Meridian ServiceMonitor is scraped. Prometheus + Alertmanager on the
  fixed NodePorts above.
- **Manifests** (`infra/manifests/`): `meridian` namespace, the three Deployments+Services+one
  ServiceMonitor, and the **read-only** k8s MCP RBAC — ServiceAccount `aegis-k8s-mcp` bound to a Role
  granting only `get/list/watch` on pods, pod logs, events, services, deployments, replicasets in
  `meridian`. **No write verbs** (ESD §16, PRD NFR-Security); V1.5 adds writes separately.
- **Automation:** `infra/setup-cluster.sh` (idempotent bring-up) + `infra/inject-failure.sh`
  (error / latency / clear / killpod) + `infra/README.md`.

### Architecture review (per CLAUDE.md §2 — new external dependency: k8s + Prometheus)
- **Reliability:** setup script is idempotent (`upgrade --install`, cluster-exists check). MCP servers
  degrade gracefully when a source is down — enforced/tested in M3.
- **Safety:** MVP RBAC is strictly read-only; the SA cannot delete or patch anything. Verified below.
- **Security:** the SA credential is scoped to one namespace; no cluster-admin; no secrets access.
- **Scalability / operational:** Prometheus retention capped at 6h and resources bounded so the stack
  fits a laptop; documented tear-down.

### Tests / verification
- Cluster up; all Meridian pods Ready; Prometheus target `up{namespace="meridian"}` returns the three
  services; a `POST /admin/failure error` visibly raises `http_requests_total{status="500"}`.
- RBAC negative check: `kubectl auth can-i delete pods --as system:serviceaccount:aegis-system:...`
  returns **no**.

### How to run
```bash
export KIND=~/bin/kind.exe HELM=~/bin/helm.exe
export KUBECTL="/c/Program Files/Docker/Docker/resources/bin/kubectl.exe"
bash infra/setup-cluster.sh
```

### Verified (actual)
- 6/6 Meridian pods Ready; Prometheus `up{namespace="meridian"}` = 6 targets.
- Injected `error rate 0.7` on checkout → Prometheus showed ~670 `status="500"` vs ~1620 `status="200"`
  over 1m, while `payment-service` stayed at 0 500s (per-service isolation holds).
- RBAC: `auth can-i` for the SA → get pods **yes**, get pods/log **yes**, delete pods **no**,
  patch deployments **no**, get secrets **no**.
- Note: the kube-prometheus-stack config-reloader lags ~30–60s after the ServiceMonitor is created
  before Prometheus loads the meridian scrape job — expected, not an error.

### Follow-ups / open items
- `inject-failure.sh error/latency` POSTs through the Service, so it lands on one of the 2 replicas
  (partial-fleet fault). For a whole-service fault, scale to 1 replica or use `killpod`. Fine for MVP.

---

## Milestone 3 — Read-only MCP servers (k8s, Prometheus, GitHub)

**Status:** complete.

### Architecture review (pre-implementation, CLAUDE.md §2)

Triggers: one **new external dependency** (the GitHub REST API — k8s and Prometheus were reviewed in
M2) and **three new untrusted data flows** (pod logs, k8s event messages, commit messages / PR
titles / diffs — all free text authored outside Aegis).

- **Reliability (ESD §12):** every upstream call retries with exponential backoff (3 attempts) on
  *transient* failures only (connect errors, timeouts, 5xx). After exhaustion the tool returns a
  structured `ToolResult{ok=false, error_kind="unavailable"}` envelope — never an exception across
  the MCP boundary — so the investigation continues with a documented gap instead of stalling.
  Non-transient responses (4xx, malformed 200-body JSON) fail fast without retry: retrying a 404 or
  a parse error wastes the incident's time budget and can't succeed. A failure must not poison the
  server: the process stays up and subsequent calls work (fault-injection-tested).
- **Safety:** all three servers are read-only by construction — the k8s client only issues GETs, and
  the RBAC from M2 (get/list/watch, no write verbs) enforces this server-side even if the client had
  a bug. Prometheus and GitHub tools are pure queries. No autonomous action surface is introduced,
  so no new safety mechanism (lease/breaker) is required in this milestone.
- **Security (ESD §16):** per-server credentials — the k8s server reads the dedicated
  `aegis-k8s-mcp` SA token from a git-ignored file minted by `infra/gen-mcp-credentials.sh`
  (short-lived via `kubectl create token`); GitHub uses an optional `GITHUB_TOKEN` env var
  (unauthenticated works for public repos); Prometheus is local and unauthenticated by design.
  No credential is committed, hardcoded, or logged. TLS to the k8s API server verifies against the
  cluster CA (also extracted to a git-ignored file), not `verify=False`.
  **Untrusted text:** pod logs, event messages, commit messages and diffs are attacker-influencable
  free text. Every `ToolResult` carries `contains_untrusted_text`; the redaction + `<evidence>`
  delimiting pipeline (ESD §16) is applied by the *consumer* before any prompt — built in the
  redaction/correlation milestone — and these servers' outputs are flagged so that boundary cannot
  be missed silently. MCP servers do not redact themselves: redaction is a single middleware applied
  uniformly at the evidence-consumption boundary (ESD §24), not scattered per-source.
- **Scalability / cost:** clients cap response sizes (log `tail_lines` default 200, bounded list
  page sizes) so a chatty pod cannot blow the token budget downstream. GitHub unauthenticated rate
  limit (60 req/h) is acceptable for MVP local use; the token raises it when configured.
- **Operational maturity:** each server is independently runnable (`python -m
  mcp_servers.<name>.server`, stdio transport), stateless, and holds no incident context (ESD §10).
  Structured logs via `core.logging`. Shared plumbing lives in `mcp_servers/common.py` — a sibling
  *library* module, not another server's code, so CLAUDE.md §9 ("no MCP server imports another MCP
  server's code") is respected; each server imports only stdlib, httpx, pydantic, the MCP SDK,
  `common.py`, and `core` (settings/logging).

Decision recorded: tool inventory (exact `verb_noun` names and schemas) is added to ESD §3 in this
same change, since CLAUDE.md §4 points at ESD §3 as the naming source of truth.

### What changed
- **Shared plumbing** (`backend/mcp_servers/common.py` — a sibling library module, not a server):
  `ToolResult` envelope (`ok, source, tool, error_kind, error, contains_untrusted_text, data,
  attempts`), `retry_transient` (exponential backoff, 3 attempts, transient-only: connect/timeout/
  5xx), and `guarded` (classified failures → envelope; unexpected exceptions still propagate —
  no bare excepts, CLAUDE.md §3).
- **k8s MCP server** (`mcp_servers/k8s/{models,client,server}.py`): tools `list_pods`, `get_pod`,
  `get_pod_logs`, `list_events`, `list_deployments`. Plain-REST GET-only httpx client
  authenticated with the `aegis-k8s-mcp` SA token (re-read per call → rotation without restart),
  TLS verified against the extracted cluster CA. Models are token-budget-conscious summaries
  (phase/ready/restarts; CrashLoopBackOff & OOMKilled surfaced from container state).
- **Prometheus MCP server** (`mcp_servers/prometheus/`): `query_metrics`, `query_range_metrics`,
  `list_alerts`. Handles Prometheus's in-body errors (HTTP 200 + `status:"error"` → bad_request,
  no retry).
- **GitHub MCP server** (`mcp_servers/github/`): `get_recent_commits` (default **2h lookback,
  FR-2.2**), `list_pull_requests`, `get_commit_diff` (per-file patches truncated at 4,000 chars —
  token-budget guard). Optional `GITHUB_TOKEN`; unauthenticated works for public repos.
- **MCP transport:** each server is a FastMCP stdio app (official `mcp` SDK ≥1.2, added to
  pyproject): `python -m mcp_servers.<name>.server`. All 11 tools registered and listed via the
  real SDK.
- **Credentials:** `infra/gen-mcp-credentials.sh` mints a short-lived (24h default) SA token via
  `kubectl create token` plus the cluster CA into git-ignored `infra/.k8s-mcp-*` files;
  `.gitignore`, `.env.example`, `core/config.py` (new k8s/github/mcp-retry settings) and
  `infra/README.md` updated.

### Decisions
- **Retry classification:** only transient failures retry (connect error, timeout, 5xx). 4xx and
  malformed 200-bodies fail fast — a parse error can't heal and retrying burns the incident time
  budget. 404 maps to `not_found` (a valid answer, source still healthy), other 4xx to
  `bad_request`, exhausted retries to `unavailable` (ESD §12 wording).
- **`contains_untrusted_text` flag** on every envelope: pod logs, event messages, alert
  annotations, commit messages/titles/patches are flagged; redaction + `<evidence>` delimiting
  stays a single consumer-side middleware (ESD §24), not per-server logic. ESD §3 now carries the
  full MVP tool inventory table documenting this per tool.
- **`common.py` placement:** shared library beside the servers; CLAUDE.md §9 forbids importing
  *another server's* code, which this is not. Servers import stdlib + httpx + pydantic + mcp SDK +
  `common` + `core` only.
- **Clients take an injected `httpx.AsyncClient`** so contract tests run against
  `httpx.MockTransport` fixtures with zero live dependencies.

### Tests (48 passing total; ruff check + format clean)
- **Contract** (`tests/contract/`, fixtures under `tests/contract/fixtures/{k8s,prometheus,github}/`):
  pod list/detail (CrashLoopBackOff + OOMKilled surfaced), bounded logs, Warning events, replica
  health; instant vector / range matrix / firing alert parsing + in-body PromQL error → bad_request
  with exactly 1 attempt; FR-2.2 `since` window assertion, PR parsing, patch truncation at
  4,000 chars; envelope JSON round-trip with `contains_untrusted_text=true` for logs.
- **Fault-injection** (`tests/fault_injection/test_mcp_degradation.py`, ESD §22): connection
  refused ×3 → `unavailable` after exactly 3 attempts; read timeout → `SourceUnavailableError`;
  repeated 500s → 3 attempts then degrade; malformed JSON → `malformed_response` with 1 attempt;
  wrong-shape JSON → `MalformedResponseError`, no crash; 404 → `not_found`; **recovery** — same
  client succeeds on the next call after upstream returns; backoff sequence asserted as
  [0.2s, 0.4s].

### How to run
```bash
cd backend && ./.venv/Scripts/python.exe -m pytest tests -q     # 48 passed
bash infra/gen-mcp-credentials.sh                                # then set K8S_API_URL in .env
K8S_API_URL=https://127.0.0.1:<kind-port> ./.venv/Scripts/python.exe -m mcp_servers.k8s.server
```

### Verified (actual, live)
- k8s client with the **real SA token + CA** (not admin kubeconfig): `list_pods` → 6/6 meridian
  pods, `get_pod` detail (Ready=True, running), `get_pod_logs` tail=5 → 5 lines,
  `list_deployments` → 2/2×3.
- Prometheus live: `sum(up{namespace="meridian"})` → `6`; `list_alerts` parsed real
  firing/pending alerts (etcd/KubeProxy alerts present right after the node restart — transient
  and expected).
- GitHub live against a real public repo: commits and closed PRs parsed (project repo isn't
  pushed until M8, so the smoke used a public repo via `GITHUB_REPO` override).
- All 11 tools enumerate through the real MCP SDK (`app.list_tools()`).

### Follow-ups / open items
- The redaction pipeline + `<evidence>` delimiting middleware (consumer side of
  `contains_untrusted_text`) is the next natural milestone; until it exists no MCP output may be
  fed to a prompt.
- `query_range_metrics` got contract coverage but wasn't exercised live this session (instant
  query + alerts were); exercise it when the Correlation agent lands.
- Docker Desktop wasn't running at session start; first `Start-Process` launch silently failed,
  second succeeded — if the cluster seems gone, check `docker ps` before rebuilding.

---

## Milestone 4 — Redaction pipeline + evidence delimiting

**Status:** complete.

- `redaction/pipeline.py`: single deterministic pass — emails, IPv4, phones, **Luhn-validated**
  card numbers (so 16-digit trace ids survive), secret assignments (`api_key=`, `password:` …)
  and bearer tokens (bearer runs before the assignment pattern so `Authorization: Bearer <jwt>`
  redacts the jwt, not the word "Bearer"). `wrap_evidence(id, source, text)` redacts then wraps
  in `<evidence>` delimiters, **defanging embedded `</evidence>`** so untrusted text can't close
  its own data region (ESD §16); tag attributes sanitized. `EVIDENCE_RULES` is the standing
  system-prompt clause consumers prepend.
- Placement per ESD §24: one middleware applied before embed/cache/log/prompt — never after.
- Tests: 6 unit tests (PII classes, Luhn negative case, injection defang, clean-text no-op).

---

## Milestone 5 — API layer: ingestion, auth, SSE, replay

**Status:** complete.

- **Ingestion** (`POST /api/v1/incidents`, FR-1.1): three-outcome contract — exact
  `external_alert_id` retry → idempotent no-op (FR-10.1, the DB-backed test promised in M1 now
  exists); different alert id but same (service, source) active within the 5-min window →
  **merged** (FR-1.2, audit-logged); otherwise a new `open` incident with severity from the
  deterministic Triage classifier (`agents/triage/classifier.py`, FR-1.3 — criticality × kind
  matrix, unit-tested 12 ways). The commit's `pg_notify` doubles as the worker wake-up (enqueue ==
  committed open row; no separate queue table).
- **Auth** (ESD §8): bcrypt directly (passlib is incompatible with bcrypt≥4.1), access+refresh
  JWTs in httpOnly/SameSite=Strict cookies (`Secure` outside local/test), refresh rotation with
  **family-wide revocation on reuse** — new `refresh_sessions` table (migration `0002`, ESD §6
  updated), storing only SHA-256 of the token id. Failure paths return the envelope + cleared
  cookies as a direct response (a `raise` would discard cookie mutations — real FastAPI footgun).
- **Events** (`api/events.py`): Postgres LISTEN/NOTIFY hub → per-incident asyncio queues → SSE
  (`GET /incidents/{id}/stream`) with 15s keepalive comments. Push end-to-end, no polling
  (CLAUDE.md §11). Any number of API processes can host the hub; Postgres stays the only bus.
- **Replay** (`GET /incidents/{id}/replay`, FR-9): transitions + steps + messages merged into one
  ordered sequence from persisted rows only — no live infra needed (FR-9.2).
- **Error envelope** (ESD §12): `{error_code, message, incident_id}` for every 4xx/5xx incl.
  validation and unhandled exceptions (logged server-side, opaque 500 out).
- **Conventions:** new `tests/integration/` directory (ESD §21 + CLAUDE.md §9 updated) backed by
  an auto-created, Alembic-migrated `aegis_test` DB; suite self-skips without Postgres. Ruff
  per-file-ignore B008 for `api/**` (FastAPI `Depends` idiom). `Settings` now reads `.env` from
  both CWD and repo root. Webhook shared-secret guard (`INGEST_WEBHOOK_TOKEN`) off locally.
- **Tests: 75 passing** (unit + contract + fault-injection + 9 integration: idempotency, dedup
  in/out of scope, envelope shape, login/me cookies, account-enumeration parity, rotation +
  reuse-detection killing the family, list requires auth).

---

## Milestone 6 — Providers, agents, orchestrator, worker, RAG

**Status:** complete.

- **Providers** (`providers/`, Strategy — ESD §20/§24): `LLMProvider` interface with full
  per-call accounting (FR-8.2); default `StubProvider` is deterministic and **grounded by
  construction** — it can only cite evidence ids present in the prompt's `<evidence>` blocks and
  derives categories from keyword signals; ensemble-pass bias makes mixed-signal incidents
  disagree (exercising FR-3.1 honestly with zero keys). Real Claude/Groq/Ollama providers are a
  deliberate follow-up *after* the eval harness exists to measure them.
- **Gateway** (`agents/gateway.py`): `McpGateway` speaks **real MCP over stdio** to the three
  servers, each a subprocess with its own credential — the worker never holds infra credentials
  (NFR-Security). Dead server ⇒ synthesized `unavailable` envelope ⇒ documented gap.
  `FixtureGateway` for tests/eval.
- **Evidence** (`agents/evidence.py`): all evidence enters via `EvidenceStore.add`, which redacts
  + delimits exactly once — no bypass path to prompts/logs/DB (ESD §24). Snippets capped at 1500
  chars for the token budget (ESD §15).
- **Agents:** correlation (`collector.py`, FR-2.1..2.3 — temporal deploy-window × topological
  service-neighborhood correlation, explicit gap notes); RCA (`engine.py` + `scoring.py`,
  FR-3.1..3.3 — N ensemble passes, malformed-output retry-once-then-drop per ESD §12, agreement =
  0.6·category + 0.4·citation-Jaccard pairwise mean, low-agreement flagged not averaged away,
  budget degradation reduces passes); observer (`validator.py`, FR-3.2/FR-8.1 — deterministic,
  **no LLM**: citation resolution + 9 injection patterns; claims citing flagged evidence rejected).
- **Orchestrator** (`orchestrator/graph.py`, Supervisor — ESD §24): triage → correlation → rca →
  observer, with ONE observer-triggered revision (flagged evidence stripped and recorded as gaps)
  then forced finalize — bounded work per incident. Single `_persist` side-effect point per node:
  step + message + citations + SSE in one transaction (ESD §4). Finalize: investigating →
  hypothesis_formed, citations stamped observer-validated, audit entry.
- **Worker** (`worker/main.py`, ESD §4): LISTEN wake-up (the ingestion NOTIFY), claim under
  `FOR UPDATE SKIP LOCKED`, 60s reconciliation sweep for open + stale-investigating (>10 min
  silent) incidents. **Decision (ESD §25 row added below): no LangGraph Postgres checkpointer in
  MVP** — the investigation is read-only + idempotent, so crash recovery = re-run from scratch;
  agent_steps/messages/transitions already persist everything replay needs. Revisit at V1.5
  where runs gain side effects.
- **RAG** (`rag/`): 768-dim deterministic hashing embedder behind an `Embedder` Strategy (BGE is
  a drop-in later — same dim, same column); content-hash-versioned `upsert_runbook` (redacts
  before embedding); pgvector cosine search; 4 seed runbooks (`eval/runbooks/`) + `python -m
  rag.seed`; `GET /api/v1/runbooks/search` wired (completes the ESD §7 MVP surface).
- **Tests: 98 passing.** New units: scoring math (6), observer validation + injection (7), stub
  grounding (4), embedder (3). New integration: full-pipeline (fixtures→hypothesis_formed with
  validated citations), **poisoned-logs pipeline** (observer rejects once, revision strips the
  evidence, run still completes), all-sources-down (completes with ≥3 documented gaps, category
  unknown).

---

## Milestone 7 — Eval harness

**Status:** complete.

- **Corpus:** 12 hand-crafted synthetic incidents (`eval/synthetic_incidents/*.json`) covering all
  four root-cause categories across all three Meridian services, plus adversarial cases: mixed
  signals (OOM despite a fresh deploy), an unavailable evidence source (github down → gap), and
  hostile injected logs (screened by the observer, then correctly attributed on revision).
- **Harness** (`backend/tests/eval/test_rca_accuracy.py`): runs the *real* correlation → RCA →
  observer path (FixtureGateway + stub, no DB, token-free → runs per-PR in CI per ESD §23) and
  enforces PRD 9A: **accuracy ≥85%** (currently 12/12) and **hallucination rate <5%** (currently
  0 — every claim's citation must resolve). Per-scenario test ensures each yields ≥1 cited
  hypothesis (FR-3.2).
- **Fixes surfaced by eval design:** stub keywords sharpened (bare "restart" matched the healthy
  `restarts=0` summary; "commit"/"deploy" matched the "no commits found" message — absence of
  change could fabricate a change signal). Collector now phrases "repo quiet" without
  change-keywords. This is exactly the class of bug the harness exists to catch.
- 114 tests passing total.

---

## Milestone 8 — Frontend (dashboard, live incident view, replay)

**Status:** complete.

- Next.js 15 (App Router, TS strict, Tailwind), hand-scaffolded — minimal custom components
  instead of the full shadcn/ui kit (documented deviation from ESD §5: same Tailwind foundation,
  shadcn can be layered in without rework; zero `any`, eslint clean).
- **Dashboard** (`app/dashboard`): incident table push-updated over a new
  `GET /api/v1/incidents/stream` all-incident SSE endpoint (added to ESD §7 in this change — the
  list view must be push-updated too; per-incident streams already existed). Live/offline dot
  reflects the SSE connection.
- **Live incident view** (`app/incidents/[id]`): hypothesis card (category, confidence,
  agreement, low-confidence banner per PRD 10A) with **observer-validated citations rendering the
  redacted snippets**, agent activity feed, state history; refreshed on each SSE event.
- **Replay** (`app/replay/[id]`, FR-9): prev/next + scrubber over the persisted event sequence;
  current event shows its full structured detail. Pure API/DB replay — no live infra.
- **Auth**: login page; httpOnly cookies ride on `credentials: "include"` fetches and
  `EventSource(withCredentials)`; the frontend never touches a token (ESD §5/§8); 401 → /login.
- `next build` + `next lint` clean.

---

# V1.5 — Safe Autonomous Action

## V1.5 architecture review (pre-implementation, CLAUDE.md §2)

Triggers: **new autonomous action types** (restart pod, scale deployment), **new auth surfaces**
(approvals, breaker clear, kill switch), **new external dep** (Slack webhook; PagerDuty mocked).

- **Reliability:** every remediation is idempotency-keyed (`remediation_actions.idempotency_key`
  unique) so a retried execution cannot double-fire. Action execution goes through the same MCP
  retry/degradation envelope; a failed action is `status=failed` with the compensating action
  *offered*, never auto-fired (ESD §12 — a failed action may not need undoing).
- **Safety (the point of V1.5):** four independent, layered gates in front of ANY execution:
  (1) **kill switch** — persistent DB flag, checked immediately before every execution, engaged =
  nothing executes anywhere; (2) **resource lease** — partial unique index in Postgres enforces
  one active lease per target at the database level, not in application code; (3) **circuit
  breakers** — per-service Tier-1 rate limit (3/h, FR-4.2: excess forced to Tier-2) and a global
  mass-action breaker (trips when system-wide Tier-1 executions in a rolling window exceed the
  threshold — one systemic root cause must not produce an unbounded action wave, ESD §17);
  (4) **tier gates** — Tier-1 allowlist auto-executes only when the Observer validated the
  hypothesis AND blast radius is within limits (FR-8.3); Tier-2 requires a recorded human
  approval (server-side role check); Tier-3 never executes. Tier-1 additionally ships in
  **shadow mode by default** (logs what it *would* do) until explicitly enabled.
- **Security:** write-capable k8s credential is a *separate* ServiceAccount
  (`aegis-k8s-mcp-writer`) with exactly `delete` on pods and `patch` on deployments/scale in the
  meridian namespace — the read-only MVP SA is untouched, and the writer token is only mounted
  into the k8s MCP server, never the worker (NFR-Security). Approval/clear/kill endpoints check
  role server-side (`on_call_engineer`/`admin`; kill switch + clear = `admin`).
  Slack webhook URL is env-only; messages are plain-English summaries already redacted upstream.
- **Scalability/operational:** breaker events and remediation actions are ordinary OLTP rows;
  audit_log absorbs the write volume via existing partitioning. Proposal expiry
  (`expires_at`, default 30 min) prevents a stale approval queue from becoming a standing-order
  risk: an expired proposal cannot be approved or executed.

## V1.5a — Safety substrate: leases, breakers, kill switch (+ all V1.5 tables)

**Status:** complete.

- **Migration 0003**: `remediation_actions` (unique `idempotency_key`; `compensating_action`
  NOT NULL — no action without a documented undo, CLAUDE.md §17; `shadow` bool for Tier-1 shadow
  mode; `reasoning` NOT NULL per FR-4.3), `resource_leases` with the **partial unique index**
  `(type,id) WHERE released_at IS NULL`, `action_circuit_breaker_events`, `approvals`,
  `memory_summaries` (compound `(service_name, incident_type)` index, FR-7.3), and new
  `system_flags` (kill switch state — ESD §6 updated). Gotcha: inline `sa.Enum` in
  `create_table` re-issues CREATE TYPE → use `postgresql.ENUM(create_type=False)` + explicit
  create.
- **safety/resource_lease**: `acquire_lease` = INSERT … ON CONFLICT (partial index) DO NOTHING →
  None on contention; the database is the arbiter. `release` idempotent.
- **safety/circuit_breaker**: `effective_tier` promotes Tier-1→Tier-2 past 3 executions/h/service
  (FR-4.2 — narrows autonomy instead of dropping the action); global window row counts all
  Tier-1 executions, trips at threshold (default 10/10min), admin-only clear.
- **safety/kill_switch**: upserted `system_flags` row; engaged = nothing executes anywhere.
- **Settings**: all thresholds in `core/config.py` under a "safety-relevant, call out in PR"
  banner (CLAUDE.md §15). `resolution_shadow_mode` **defaults ON**.
- **Tests (117 passing):** second lease refused + reacquire after release; **4-way concurrent
  lease race → exactly one winner** (real DB race); per-service rate-limit promotion with
  cross-service isolation; global breaker trip + admin clear; kill-switch round-trip.

## V1.5b — k8s write tools + Resolution Agent

**Status:** complete.

- **Writer RBAC** (`infra/manifests/mcp-rbac-writer.yaml`): separate SA
  `aegis-k8s-mcp-writer` — `delete pods` + `deployments/scale` (get/patch/update) in meridian
  only. The read-only MVP SA is untouched; `gen-mcp-credentials.sh` mints the writer token only
  when the SA exists. ESD §3/§16 honored: `/scale` subresource = the replicas-only patch surface.
- **k8s MCP write tools**: `restart_pod`, `scale_deployment` (ESD §3 updated). Write requests get
  **one attempt, no blind retry**; scale records `previous_replicas` (feeds the compensating
  action). Missing writer token ⇒ SourceUnavailable ⇒ the write capability simply doesn't exist.
- **Action catalog** (`agents/resolution/actions.py`, FR-4.1): restart_pod=T1,
  scale_deployment=T2, rollback_deploy=T3 (mcp_server=None — structurally not machine-executable).
  Compensating action documented at definition time; the model makes an undocumented undo
  impossible. Category→action map: error_spike/unknown deliberately map to *no* action.
- **Engine** (`agents/resolution/engine.py`): idempotent `propose_remediation` (unique key =
  incident:action:target; FR-4.3 reasoning + FR-4.4 blast radius recorded pre-execution) and
  `execute_action` behind **four ordered gates**: kill switch → expiry → tier/approval →
  (Tier-1 only) observer-validated + blast-radius ≤ limit + global breaker; then lease-wrapped
  MCP call, breaker accounting, always-release. Failure ⇒ `failed` + compensating action
  *offered* (ESD §12). Tier-1 **shadow mode ON by default**: records `would_call`, touches
  nothing. `execute_compensating_action` = human-triggered scale-back.
- **Orchestration**: new `resolution` graph node after finalize (proposes; Tier-1 executes
  inline); worker sweep executes **approved** Tier-2 actions (approval in API, execution in
  worker — ESD §11) under `FOR UPDATE SKIP LOCKED`.
- **Tests (130 passing):** catalog invariants; shadow-executes-nothing; real Tier-1 restart →
  monitoring; kill switch blocks; blast-radius gate escalates; unvalidated hypothesis never
  auto-executes; **Tier-2 scale 2→3 then compensating action restores 2 (stateful fake — the
  reversal is proven on state, not on call counts)**; expired proposal refused; proposal
  idempotency.

## V1.5c — Approvals API, Communication Agent, memory, Slack + PagerDuty-mock MCP

**Status:** complete.

- **Approvals** (FR-5): `GET /approvals` queue; `POST /incidents/{id}/approvals` — server-side
  role check (viewer 403-tested), pending-only, expiry-checked (409 `proposal_expired`), Tier-3
  approval refused outright (`tier3_manual_only`); approve → `remediation_approved` transition,
  reject → terminal `rejected` (FR-5.2, never auto-retried). **Execution stays in the worker**
  (sweep picks up `approved` under SKIP LOCKED) — the API never touches infrastructure (ESD §11).
- **Safety endpoints**: breaker status (any role) / clear (admin); kill switch — engage
  on_call+admin, **disengage admin-only**.
- **Communication Agent** (FR-6): deterministic plain-English templates for all five FR-6.2
  phases; unit test asserts NO jargon token (pod/k8s/5xx/p99/…) can leak into a stakeholder
  update. Wired: opened (triage node — FR-6.1 ≪2min), root_cause (finalize, validated-only),
  proposed/executed (resolution node + worker), resolved (resolve endpoint). Slack mirroring is
  best-effort via the new **slack MCP server** (webhook from env; unconfigured = clean
  non-delivery, never a blocker).
- **Memory** (FR-7, on Postgres instead of Mem0 — same interface, one less dependency; ESD §25
  candidate noted): `draft_summary` on resolution (symptom/root-cause/fix/outcome,
  `approved_by NULL` = pending), `POST /memory/{id}/approve` with field-whitelisted edits,
  `recall` returns **approved-only**, compound-key scoped (FR-7.3); RCA node now folds recalled
  memories into its context (FR-3.3).
- **New endpoints + state machine**: `POST /incidents/{id}/resolve` (also the MVP-gap fix — FR-9
  wanted resolved incidents; nothing could resolve one); **state-machine change (called out per
  CLAUDE.md §15):** added `remediation_proposed → resolved` so a rejected proposal doesn't strand
  the incident. ESD §6.1/§7 updated.
- **PagerDuty-mock MCP server**: fixture-replay engine over `eval/pagerduty_fixtures/`.
- **Tests: 148 passing** (communication templates ×3, slack/pagerduty contract ×3, V1.5 API ×8:
  RBAC, approval flow incl. double-decide 409, rejection→manual-resolve, expiry, kill-switch role
  split, breaker trip/clear via API, memory draft→gate→scoped recall, edit-field whitelist).

## V1.5d — V1.5 frontend, monochrome design system, marketing homepage

**Status:** complete. **V1.5 is complete.**

- **Design system rebuild** (`globals.css` + `tailwind.config.ts`): two themes only — **pure
  black (#000) and pure white** — expressed as CSS-variable RGB triplets so Tailwind opacity
  modifiers keep working (`bg-surface/60`). Semantic class vocabulary (`bg`, `surface`,
  `surface2`, `edge`, `fg`, `muted`, `inverse-bg/fg`) is shared by both themes, so no component
  branches on theme. **Zero blue/indigo/sky/violet/cyan/teal in the codebase** (grep-verified);
  the only chromatic values are the P1→P4 severity ramp and ok/danger — an incident tool that
  renders P1 and P4 identically has thrown away information the operator needs.
- **Theme switch** (`ThemeToggle`): writes `<html data-theme>` + localStorage; an inline script
  in `layout.tsx` applies the stored choice **before first paint**, so there is no flash of the
  wrong theme. Falls back to the OS `prefers-color-scheme` on first visit.
- **Marketing homepage** (`app/page.tsx`, public, Apple-style): sticky translucent nav, hero at
  `clamp(2.75rem, 8vw, 6.5rem)` with tight tracking, a **CSS-drawn product still** (no
  screenshots to go stale, renders correctly in both themes), numbers band, four-step
  "how it works", a cited-evidence panel, the four safety gates, the agent cast, and a closing
  CTA. `AppShell` detects `/` and steps aside so marketing renders its own chrome.
- **V1.5 app surfaces**: approvals queue (reasoning + blast radius + the documented undo, with
  role-gated buttons — cosmetic only, the server enforces), safety page (kill switch + breaker;
  red used *only* where the meaning is literally "stop"), and a "Mark resolved" action on the
  incident view.
- **Bug found and fixed during visual verification:** after login the header still showed
  "Sign in" — `router.push` kept the shell mounted, so `UserBadge`'s mount-time `/auth/me` never
  re-ran. Login now does a full navigation.
- **Also hit:** running `next build` against a live dev server corrupts its `.next` cache
  (`ENOENT vendor-chunks/next.js`, page renders unstyled). Fix: stop the server, `rm -rf .next`,
  restart. Worth knowing before assuming a CSS regression.
- Verified in-browser in both themes: marketing homepage, dashboard (real incidents from the
  live E2E runs), incident detail with observer-validated citations. `next build`, `next lint`,
  `tsc --noEmit` all clean; backend still 148 passing, ruff clean.

---

## Real LLM integration — OpenRouter, and the removal of every fake path

**Status:** complete (accuracy re-measurement pending upstream capacity; see below).

### Real models, no offline path
- **`providers/openrouter.py`**: OpenAI-compatible client with two independent recovery axes —
  **key rotation** across the configured keys, and a **model fallback chain** — because on free
  tiers throttling is the normal case, not an edge case. Real token/cost accounting comes from the
  API `usage` block, so FR-8.2 numbers and the ESD §15 budget are actual, not estimated.
- **Model choice** (measured, not guessed): probed five free models with a real RCA prompt.
  `nvidia/nemotron-3-super-120b-a12b:free` — 5.2s, correct category, all three citations resolving
  — is the RCA model; `nemotron-nano-9b-v2` handles the cheaper agents; gemma/gpt-oss are
  fallbacks. `google/gemma-4-31b-it:free` was already 429ing during the probe, which is precisely
  why the fallback chain exists.
- **Every stub/fake/mock path deleted from the product** (user directive, and the right call):
  `providers/stub.py` gone; `llm_degrade_to_stub` gone; the factory accepts **only** `openrouter`;
  `FixtureGateway` moved out of `agents/` into `tests/support/doubles.py`; the **PagerDuty-mock
  MCP server and its fixtures deleted** (it served fabricated incidents while real alerts already
  arrive from Prometheus/Alertmanager); the marketing homepage's mocked-up incident replaced with
  a pipeline schematic, and the remaining example panel labelled as an illustration.
  When every model and key is exhausted the provider now raises `ProviderExhausted` and the
  incident is left for the retry sweep. **Degrading throughput is fine; degrading truthfulness is
  not** — recorded as an ESD §25 trade-off.

### Three defects the real model exposed (all fixed)
Running the real pipeline surfaced problems the deterministic stub structurally could not:
1. **Unsupported causes passed validation.** The model asserted *"a recent deployment introduced a
   bug"* while GitHub was unavailable and **no deploy evidence existed at all**. Every citation
   resolved, so the Observer approved it. Added `check_category_support`: a category may only be
   asserted when the cited evidence actually supports it — `deploy_regression` now requires cited
   `diff` evidence, `resource_exhaustion` a real OOM/crash signal, and so on. RCA is also told up
   front which causes are unassertable when their source is missing.
   *My first version of this guard had its own bug — naive substring matching meant `restarts=0`
   satisfied the "restart" marker, so a perfectly healthy pod supported a resource-exhaustion
   claim. Markers are now regexes requiring a non-zero fault signal; the test that caught it is
   `test_resource_exhaustion_needs_an_oom_or_crash_signal`.*
2. **One surviving pass reported as unanimity.** Real prompts plus hidden reasoning tokens
   overran `max_tokens=900`, truncating the JSON mid-object; 2 of 3 ensemble passes were silently
   dropped, and the survivor scored `agreement 1.00` — one opinion presented as corroboration.
   Fixed on both ends: budget raised to 2500 with an automatic re-ask at 4000 on
   `finish_reason == "length"` (the caller's `max_tokens` was also being ignored entirely), and a
   degraded ensemble is now **always** flagged low-confidence regardless of score.
3. **Real-model JSON shapes.** Fenced blocks, prose-wrapped objects, `"85%"` confidences and
   malformed claim entries all broke parsing. `providers/parsing.py` (provider-neutral, so agents
   don't couple to a vendor) recovers them without spending a retry.

### Measured (real models, genuine calls)
- Pre-fix full-corpus run: **12/12 accuracy, 0 hallucinated citations across 43 claims**,
  35,873 tokens, 13–81s per scenario.
- Adversarial checks: with near-zero evidence the model answered **`unknown`** rather than
  inventing a cause; a fabricated `E99` citation was **rejected** by the Observer.
- Live E2E: real 80% error injection on `checkout-service` → real Prometheus/k8s/GitHub MCP
  evidence → 5 real RCA calls → observer-validated hypothesis → Tier-3 `rollback_deploy` proposed
  (correctly *not* auto-executed).
- **Re-measurement after the grounding fixes is blocked on quota, not on code.** All four keys
  returned `Rate limit exceeded: free-models-per-day` — OpenRouter's free tier allows ~50
  requests/day per account, and this session spent them on model probing, two 36-call corpus runs
  and the live E2E. The cap resets at UTC midnight; adding ~10 credits to the account raises it to
  1000/day. The provider now raises a distinct `DailyQuotaExhausted` carrying exactly that
  guidance, because a daily cap and a per-minute throttle need completely different operator
  responses and collapsing them sends people to re-run a command that cannot succeed for hours.
  Re-run when quota returns: `backend/.venv/Scripts/python.exe eval/run_real_eval.py`.

### Tests
166 passing. New: `tests/unit/test_grounding.py` (14 — category support, the healthy-pod false
positive, degraded ensembles, real-model JSON shapes) and `tests/unit/test_no_fabrication.py`
(9 — the stub module is *gone* not just unregistered, the factory refuses every non-real provider
name, an exhausted provider raises instead of answering, and a fully-throttled key×model matrix
still refuses to invent output).

---

## V2a — Firebase Authentication + Google OAuth (architecture review)

New external dependency (Google Identity Platform) and a **new auth surface**, so CLAUDE.md §2
requires this review *before* implementation code. Findings, in the five standing categories.

### Reliability
- **Google is now in the login path.** If Identity Platform is unreachable, nobody can obtain a
  new session. Mitigation: the exchange is a *one-time* cost. Once the httpOnly session cookie is
  minted, every subsequent request is verified against our own JWT secret with no Google call, so
  an outage degrades to "no new logins" rather than "everyone is ejected".
- **Public-key fetch.** Admin-SDK ID-token verification needs Google's rotating x509 certs. The SDK
  caches them; a cold start during a Google outage fails closed (401), never open.
- **Clock skew** breaks JWT `iat`/`exp` validation in both directions. Verification uses the SDK's
  default tolerance rather than a hand-rolled comparison.

### Safety
- Auth is upstream of the V1.5 approval gates. A bug that over-grants a role converts into
  unauthorized cluster writes, so role assignment **fails closed**: an email absent from
  `AEGIS_APPROVER_EMAILS` is provisioned `viewer`, which cannot approve anything. There is no code
  path where an unrecognized Google account receives `on_call_engineer` or `admin`.
- The four execution gates (kill switch → expiry → tier/approval → observer+blast-radius+breaker)
  are untouched by this change. Auth decides *who is asking*, never *whether the action is safe*.

### Security
- **The §12 non-negotiable is preserved by design, not by accident.** The Firebase client SDK
  stores ID tokens in IndexedDB, which JavaScript can read. So the client SDK is used *only* to
  complete the Google popup; the ID token is POSTed once to `/auth/session` and the client is
  signed out immediately, leaving nothing JS-readable. The browser's durable credential remains
  our existing httpOnly/Secure/SameSite=Strict cookie.
- **ID tokens are verified, never trusted.** `verify_id_token` checks signature, issuer, audience
  (must equal our project id) and expiry server-side. A client-supplied `email` or `uid` field is
  ignored entirely; identity comes only from verified claims.
- **`email_verified` is enforced.** Google accounts always set it, but a federated provider on the
  same Firebase project might not, and an unverified email would defeat the allowlist.
- **Service account key is a high-value secret.** It grants project-wide Firebase admin. Stored in
  a gitignored file referenced by path via env var — never inlined in code, never logged, never
  committed. `.gitignore` was extended before the key was written to disk.
- **Shared Firebase project.** *(Resolved in V2b — see below.)* The credentials supplied for V2a
  belonged to a non-Aegis project, so Aegis shared a user pool with another application: anyone
  who could sign into that app was authenticated (though not authorized) here. The allowlist was
  what made it acceptable, and a dedicated project was recorded as the correct end state. V2b
  migrated to the dedicated `aegis-ai-detective` project, closing this.
- **Account-takeover blast radius.** Compromise of an allowlisted Google account now yields
  approval rights. This is a real reduction in defense-in-depth versus a password we control, and
  is accepted deliberately: Google's own MFA and anomaly detection are stronger than anything this
  project would implement, and the allowlist bounds the exposed set to named individuals.

### Scalability
- Verification is signature-checking against cached public keys: constant-time, no per-request
  network hop, no shared state. It does not constrain horizontal scaling.
- User provisioning on first sign-in is an upsert keyed on the unique `email` column, so
  concurrent first-logins from the same account collapse to one row rather than racing.

### Operational maturity
- Role changes are an env-var edit plus a restart. Acceptable at this scale, and it keeps
  authorization reviewable in one place rather than mutable through a UI. A promotion flow is V2+.
- Revocation has a bounded window: removing an email from the allowlist does not invalidate an
  already-issued access token, so the existing 15-minute access TTL is the revocation SLA.
  Refresh-family revocation remains the immediate lever.
- Loss of the service account key is recoverable (rotate in console, update the file); no Aegis
  data is encrypted with it.

### Decisions carried into implementation
1. Token exchange, not client-held tokens — §12 stands unamended.
2. `AEGIS_APPROVER_EMAILS` allowlist grants elevated roles; everyone else is `viewer`.
3. The existing refresh-rotation + family-revocation machinery is retained; Firebase replaces only
   the *credential presentation* step, not session management.
4. `users.hashed_password` becomes nullable — a federated user has no password. Per CLAUDE.md §15
   this is a schema change requiring a migration, not a repurposed column.

### V2a — as built

**Firebase auth.** `api/firebase_auth.py` is the only place a Google identity enters the system.
It returns a verified identity or `None` — there is no branch that returns an identity on an
error path, so every failure mode (malformed, wrong project, expired, revoked, unverified email,
Firebase unreachable) denies. `firebase_admin`'s verification is synchronous and may fetch
Google's rotating certs, so it runs via `anyio.to_thread` rather than holding the event loop
during someone else's TLS handshake. `FirebaseConfigError` is separated from a rejection and
surfaces as **503**, not 401: a misconfigured deployment should not send users off to re-check a
password they do not have.

**Redis.** `core/redis.py`. The client is cached **per event loop**, not per process —
`redis.asyncio` binds connections to the creating loop, and a process-global client raised
"Event loop is closed" across a test suite with a loop per test. Loop-awareness is the real fix;
closing the client in a test fixture would have hidden a genuine multi-loop hazard.

**Defect found during live verification.** The rate limiter returned 500 rather than 429.
`@app.middleware("http")` executes outside the `ExceptionMiddleware` that FastAPI installs, so a
raised `AegisError` is never converted by the registered handlers. The pre-existing
`webhook_guard` shared the defect and would have returned 500 instead of 401 for a bad webhook
token — it was never caught because no test asserted the status. Both now return
`errors.error_response(...)`, and `tests/integration/test_middleware.py` asserts the exact status
and `error_code` for each.

**Measured live (real dependencies, genuine calls)**
- Firebase Admin initialized against the then-current project; a forged token was rejected with
  `InvalidIdTokenError`. The token itself never appears in a log line. *(The project was replaced
  in V2b; the verification was re-run against `aegis-ai-detective`.)*
- `POST /auth/login` → **404** (password auth removed, not merely bypassed).
- 120 requests allowed, then a real **429** with `Retry-After: 60` and the error envelope.
- Redis stopped → `/health` reports `degraded` at **200** (not 503) and the API keeps serving;
  the limiter allowed 20/20 requests that would otherwise have been throttled. Restarted → `ok`.
- Live Redis keys inspected in-container: `ratelimit:127.0.0.1:GET:/api/v1/incidents` = `2`,
  TTL 58s.
- **188 tests passing**, ruff clean; frontend `tsc --noEmit`, `next lint`, `next build` all clean.

**Not done, and not claimed.** The runbook embedder remains the non-semantic `HashingEmbedder`
(BGE swap agreed but not started), and there is still no LLM streaming.

**Requires operator action.** Google sign-in must be enabled in the Firebase console
(Authentication → Sign-in method) and `localhost` present under Authorized domains; the popup
flow could not be exercised here because it requires the operator's own Google credentials.

---

## V2b — Dedicated Firebase project, semantic embeddings, production RAG

### Firebase migration
Full replacement, not an incremental edit. The old project's service account, project id and all
six `NEXT_PUBLIC_FIREBASE_*` values were swapped for the dedicated `aegis-ai-detective` project.
A repo-wide sweep for every old value (project id, API key, app id, sender id, storage bucket,
auth domain) returns nothing outside two historical journal notes, which were **amended rather
than erased** — these logs are append-only (CLAUDE.md §19), but a note that had become factually
false could not be left standing.

Notably the migration touched **no source file**: every credential already lived in gitignored
env/secret files, which is the design working as intended.

### Environment defect
The backend venv had been recreated on **Python 3.14** at the old repo path while the installed
packages were `cp312` builds. The symptom was three apparently unrelated failures —
`pydantic_core._pydantic_core`, `asyncpg.protocol.protocol`, `xxhash._xxhash` — none of which
names the actual cause. Rebuilt on 3.12.10 per the Entry 1 pin.

### Embeddings
`HashingEmbedder` deleted. Local ONNX **BGE `bge-small-en-v1.5`** via `fastembed`, chosen for the
reasons in the ESD §25 rows: same model quality as sentence-transformers for ~90MB of deps rather
than ~2.5GB of torch, and no network hop on the retrieval path. Async thread-offload; configurable
model/dim/batch; a load-time dimension guard because the alternative failure surfaces as an opaque
pgvector error at INSERT. Query/passage asymmetry honoured (`query_embed` vs `embed`).

### RAG
Retrieval moved from documents to **chunks**. Structure-aware Markdown chunking (heading trails
carried into the embedded text), hybrid pgvector + Postgres full-text fused with **RRF**, metadata
filtering pushed *inside* both retrievers, cross-encoder reranking over an over-fetched pool,
configurable top-k, section-level citations, and independent degradation at every stage.

### Three real defects found and fixed during implementation
1. **Silent empty index.** Freshness was decided on the content hash alone. Immediately after
   chunking landed, re-ingestion logged `changed=false` for all four runbooks and produced **zero
   chunks** — every RAG query would have returned nothing while ingestion reported success. Fixed
   by making freshness content AND model AND dimension AND chunks-exist (migration `0006` records
   index provenance). Regression-tested both ways.
2. **Short sections dropped.** `min_chars` was applied to whole sections, discarding terse but
   complete instructions ("Raise the memory limit and redeploy") — the most actionable content in
   the corpus. Now applied only to fragments produced by splitting.
3. **Stuttering citations.** Rendered "Runbook: OOM › Runbook: OOM" because a document's H1 is
   usually also its title. Regression-tested.

### Measured live (real model, real Postgres, real corpus)
- Firebase Admin initializes against `aegis-ai-detective`; forged token rejected
  (`InvalidIdTokenError`). `POST /auth/login` → 404.
- Query *"containers keep dying because they used too much RAM"* → OOM runbook **top-1**, with
  **zero shared vocabulary** with the document. This is precisely the query the hashing embedder
  could not serve.
- Query *"OOMKilled"* → matched by **`semantic,lexical`** together, confirming both retrievers
  contribute rather than one silently dominating.
- `service=catalog-service` filter → only chunks tagged for that service.
- **211 tests passing**, ruff clean.

### Constraint surfaced
The shipped corpus is four ~650-char documents with no subheadings, so it yields one chunk each.
Chunking is correct but its value is unexercised at this corpus size; multi-section behaviour is
covered by unit and integration tests using structured fixtures. A larger corpus is what would
make the chunking investment visible in production numbers.

---

## V2c/V2d/V2e — AI layer: real agents, LangSmith tracing, evaluation framework

### The audit that started it
Of seven "agents", **one** called an LLM. Triage was two `set` lookups; Correlation was five
hardcoded MCP calls in fixed order; Observer was regexes; Communication was `str.format()`;
Resolution was a dict lookup; the "Supervisor" was static edges with one boolean conditional.
`langchain-core` and `langsmith` were installed and imported in **zero files**.

A safety carve-out was agreed before any conversion: Observer citation-resolution, redaction,
the four execution gates, the ingestion severity floor and the state machine stay deterministic.
A watchdog must not share the failure modes of the thing it watches; an LLM redactor can be
injected into not redacting; an LLM deciding "is this action permitted" turns an injected log
line into cluster-write authorisation.

### What became genuinely AI
- **Triage** (`agents/triage/reasoner.py`) — reasons about customer impact, but **clamped**: it
  may raise severity above the rule-based floor, never lower it. Escalation is judgment;
  de-escalation is an attack surface.
- **Correlation** (`agents/correlation/planner.py`) — a real plan → dispatch → observe → re-plan
  loop. The model picks tools from a **read-only allowlist** (`tools.py`); Python dispatches, so
  evidence gathering is structurally incapable of reaching `restart_pod`/`scale_deployment`.
  Rejected calls are returned *with a reason* — a silently dropped call looks to the model like a
  tool that returned nothing, and it will reason from that absence.
- **Supervisor** (`orchestrator/supervisor.py`) — routing is an LLM decision over a genuine graph
  cycle. `available_steps()` computes the legal set from state; an out-of-set choice is
  overridden. The model can request another RCA pass; it cannot exceed the revision limit or
  token budget, reach `resolution` without a validated hypothesis, or refuse to finalize.
- **Communication**, **Observer critique** — LLM-written / LLM-judged, the critique running
  *alongside* deterministic validation. It can veto, never rescue: an LLM persuaded by injected
  text must not approve what the machinery refused.

### Bugs found while building (all regression-tested)
1. **Alert kind was discarded at ingestion.** `AlertIn.kind` set the provisional severity and was
   then thrown away, so the graph called `classify_severity(service, "error_rate")` with the kind
   **hardcoded** — every incident triaged as an error-rate alert whatever fired. Migration `0007`.
2. **Infinite loop when all sources are down.** Gating correlation on *evidence gathered* rather
   than *having run* made it the only legal step forever. Hit LangGraph's recursion limit.
3. **Unbounded "gather more evidence".** `correlation` stayed permanently available after a
   rejected hypothesis; it is always a plausible next step, so a model favouring it never
   concludes. Now capped by `correlation_max_invocations`.
4. **Incidents could finish without finalizing.** The supervisor could route to `resolution`,
   which had a direct edge to `END` — no state transition, no audit record.
5. **`CritiqueResult.verdict` was a bare `str`** with allowed values stated only in prose, so
   structured-output enforcement could not constrain it. Now a `Literal`.

### LangSmith (`core/tracing.py`)
Traces every graph node, LLM call (model actually used after fallback, tokens, cost, latency,
repair attempts) and MCP tool call, with one trace per investigation tagged by `incident_id`.
**Every entry point is a no-op when unconfigured** — an observability outage must not stall an
investigation. Needs `LANGSMITH_API_KEY`; unset today, so tracing is inert but wired.

### Evaluation framework (`evaluation/`)
Split by what each metric **costs to run**, which decides where it can live:
- `retrieval_metrics.py` — hit rate, MRR, precision@k, NDCG@k, forbidden rate. Pure arithmetic:
  no model, no network, no quota. Gates every commit via
  `tests/integration/test_retrieval_quality.py`.
- `ragas_metrics.py` — faithfulness, answer relevancy, context precision. LLM-judged, so
  on-demand. **RAGAS is an optional extra** (`pip install -e ".[eval]"`), not a runtime
  dependency: it pulls ~37 packages (pandas, pyarrow, datasets, langchain, openai) and downgrades
  `fsspec`, which the embedding stack depends on. Shipping that into the production image to
  support a CI-only measurement is a poor trade.

### Measured, against the REAL shipped corpus (4 runbooks)
`cases=7 hit_rate=0.86 mrr=0.65 p@k=0.21 ndcg@k=0.70 forbidden=0.14`

The framework immediately found weaknesses the test fixture masked:
- **`unanswerable` FAILS.** An out-of-domain query ("how do I file an expense report") still
  returns confident operational runbooks. **`rag_min_score` defaults to `0.0`, so the relevance
  floor is disabled** and retrieval always returns `k` results however irrelevant — feeding
  distractors straight into the RCA prompt. This is a real production weakness, not a test
  artefact.
- **`latency-symptom` FAILS on the distractor check.** The latency runbook ranks #1 correctly,
  but the OOM runbook still appears inside top-5.
- **`deploy-regression` rr=0.25, `availability` rr=0.33** — correct document found, but ranked
  4th and 3rd. Hit rate hides this; MRR is why it is measured.
- `p@k=0.21` is expected at k=5 on a 4-document corpus (at most one document is relevant), and
  is not meaningful until the corpus grows.

**These numbers are the current honest baseline, not a target.** They are recorded so the next
change to chunking, fusion weights, the embedder or the reranker can be judged against them.
