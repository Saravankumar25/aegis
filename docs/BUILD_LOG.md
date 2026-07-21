# BUILD_LOG.md — Aegis Development Journal

The detailed, running record of how Aegis is built, milestone by milestone. This is the companion
to CLAUDE.md's Feature Log (which keeps brief pointers): here we capture *what* was built, *which*
PRD FR/NFR and ESD sections it satisfies, the *decisions* made, the *tests* added, and *how to run*
each piece. Source-of-truth hierarchy: PRD.md → ESD.md → CLAUDE.md → this log.

---

## Current status / resume here (as of 2026-07-21)

**Done & committed:** M0 (rename + scaffold), M1 (data layer), M2 (kind cluster + Meridian +
Prometheus), M3 (read-only MCP servers — see the Milestone 3 section below). Nothing pushed yet
(push is M8, to repo `aegis`).

**Next up: Milestone 4** — the natural next step is the redaction pipeline + evidence delimiting
(ESD §16), since every downstream consumer of MCP evidence needs it; then agents/orchestrator
(M5 pairs the ingestion API with the DB-backed idempotency test, per the M1 note). The MCP
servers' `contains_untrusted_text` flag is the hook the redaction boundary consumes.

**Environment state to reconnect tomorrow:**
- Tool binaries: `KIND=~/bin/kind.exe`, `HELM=~/bin/helm.exe`,
  `KUBECTL="/c/Program Files/Docker/Docker/resources/bin/kubectl.exe"`. Backend venv is Python 3.12
  at `backend/.venv`.
- **Postgres/Redis:** `docker compose up -d` (Postgres on host **5433**, migration already applied to
  the volume — re-run `alembic upgrade head` is a no-op).
- **kind cluster `aegis`:** may still be running from today. Re-check with
  `kubectl --context kind-aegis get pods -A`; if gone, `bash infra/setup-cluster.sh` rebuilds it
  (~10 min). Prometheus at http://localhost:9090 once up.
- Quick sanity: `cd backend && ./.venv/Scripts/python.exe -m pytest tests -q && ./.venv/Scripts/python.exe -m ruff check .` → 48 passing, clean.
- **k8s MCP credential:** `bash infra/gen-mcp-credentials.sh` mints the git-ignored SA token/CA
  (token lives 24h — re-mint each session); it prints the `K8S_API_URL` (kind's mapped port
  changes if the cluster is recreated).

**Open threads:** none blocking. M5 will add the DB-backed idempotency test for `upsert_incident`;
GitHub push (M8) will need a PAT or `gh` (not installed).

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
