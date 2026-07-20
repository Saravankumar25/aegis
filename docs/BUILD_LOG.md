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
