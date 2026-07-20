# BUILD_LOG.md — Aegis Development Journal

The detailed, running record of how Aegis is built, milestone by milestone. This is the companion
to CLAUDE.md's Feature Log (which keeps brief pointers): here we capture *what* was built, *which*
PRD FR/NFR and ESD sections it satisfies, the *decisions* made, the *tests* added, and *how to run*
each piece. Source-of-truth hierarchy: PRD.md → ESD.md → CLAUDE.md → this log.

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
