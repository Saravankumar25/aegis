# Aegis

Multi-agent incident-response system. Seven agents (Triage, Correlation, RCA, Resolution,
Communication, Observer, Orchestrator) investigate — and, in V1.5, safely remediate — production
incidents on a simulated e-commerce company, **Meridian Commerce**, running on a local Kubernetes
(`kind`) cluster.

> **Current phase: MVP** — investigation only, fully read-only. No production infrastructure is
> modified. See [docs/PRD.md](docs/PRD.md) for requirements and [docs/ESD.md](docs/ESD.md) for
> architecture. Day-to-day working rules live in [CLAUDE.md](CLAUDE.md); the running build journal
> is [docs/BUILD_LOG.md](docs/BUILD_LOG.md).

## What the MVP does

An alert fires → Aegis creates an incident (idempotently) → the **Triage → Correlation → RCA**
pipeline gathers real evidence (k8s, Prometheus, GitHub) and forms a root-cause hypothesis with a
confidence score, where **every claim cites specific evidence** and the **Observer** rejects any
claim it can't validate. A Next.js dashboard shows the investigation live over SSE, and a replay
mode steps through any resolved incident from the persisted audit log.

## Repository layout

```
backend/    FastAPI + LangGraph agent pipeline, MCP servers, RAG, redaction (see ESD §21)
frontend/   Next.js dashboard (live SSE feed + replay mode)
infra/      kind cluster, Meridian Commerce services, kube-prometheus-stack, CI
eval/       synthetic incidents + postmortem corpus for RAG/eval
docs/       PRD.md · ESD.md · BUILD_LOG.md
```

## Local development

```bash
cp .env.example .env           # fill in local values (never commit .env)
docker compose up -d           # Postgres (pgvector) + Redis

cd backend
python -m venv .venv && . .venv/Scripts/activate   # use Python 3.12 (see BUILD_LOG)
pip install -e ".[dev]"
alembic upgrade head
pytest                          # unit + contract + fault-injection
ruff check .
```

**Aegis ships no stub, mock, or offline LLM provider.** Every agent output comes from a real
model call through OpenRouter (`LLM_PROVIDER=openrouter`), so working API keys are required —
without them the system fails loudly rather than fabricating an analysis that would be
indistinguishable from a real one at the moment a human decides to trust it. See
[ESD §20](docs/ESD.md) and the trade-off row in ESD §25.

Sign-in is **Google OAuth via Firebase Authentication**. The browser's session credential is
an httpOnly cookie Aegis issues itself; the Firebase ID token is exchanged once at
`POST /auth/session` and never persisted client-side (ESD §8). Copy `.env.example` → `.env`
and `frontend/.env.example` → `frontend/.env.local`, then place the Firebase service-account
JSON at the gitignored path named by `FIREBASE_SERVICE_ACCOUNT_FILE`.

Authorization is a fail-closed allowlist: `AEGIS_ADMIN_EMAILS` and `AEGIS_APPROVER_EMAILS`
grant elevated roles, and any other authenticated Google account is provisioned `viewer`,
which cannot approve a remediation.

## Status

Under active construction, milestone by milestone. Progress is tracked in
[docs/BUILD_LOG.md](docs/BUILD_LOG.md) and in the Feature Log at the end of [CLAUDE.md](CLAUDE.md).
