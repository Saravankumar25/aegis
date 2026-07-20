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
