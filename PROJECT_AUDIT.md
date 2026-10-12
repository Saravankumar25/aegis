# Aegis 2.0 — Project Audit (Phase 0)

Audit date: 2026-10-10 · Commit audited: `579e461` on `main` · Scope: read-only. No application code, infra, plugins, branches or caches were changed.

**Evidence labels.** `VERIFIED` = observed in code or by running a command in this audit. `SUSPECTED` = inferred from file names, partial reads or docs; needs a follow-up check. `NOT CHECKED` = deliberately out of scope (e.g. live AWS calls).

**Commands actually run** (all passed unless noted):

| Check | Result |
|---|---|
| `pytest tests` (backend, full) | **1263 passed, 17 skipped** (1280 collected). Skips are integration tests with no datastore. |
| `ruff check src tests` | clean |
| `mypy src` | clean, 164 source files |
| Frontend `npm run lint` / `typecheck` | clean |
| Frontend `npm run test` (vitest) | 9 files, 92 tests passed |
| Not run | `npm run build`, `eval/run.py --suite smoke` (may spend LLM budget), `terraform`/`aws` (no live calls) |

---

## 1. Project overview and actual architecture

Aegis is an AI-native SRE platform: alert in → investigation (LangGraph) → evidence-cited diagnosis → gated remediation → verification → commit/rollback. The distinguishing design is that the write path is structurally gated (`ValidatedAction` minted only by `ActionGate`; tier-3 actions have no executor, asserted at import).

**Actual shape (VERIFIED):**

```
alert -> POST /v1/alerts (ingest token) -> incident + workflow_jobs row (one txn)
      -> worker (FOR UPDATE SKIP LOCKED, leases, poison-job parking)
      -> LangGraph workflow, 13 nodes (not 12) ; Horizon orchestrator (second runtime)
      -> 36 MCP tools (35 read, 1 write) -> ActionGate -> executor -> verify
Postgres = record · Neo4j = topology · Redis = non-authoritative
Frontend: Next.js 15 / React 19 console, SSE war-room
```

| Package | Files | ~LOC | Note |
|---|---|---|---|
| agents | 25 | 9.4k | workflow.py plus undocumented `agents/horizon/` |
| mcp | 15 | 6.2k | tool boundary |
| integrations | 10 | 5.6k | runtime adapters, github, slack, langsmith |
| api | 24 | 5.0k | routers under `/v1` |
| evaluation | 17 | 4.9k | harness + ablations |
| execution | 10 | 3.9k | gate chain, leases, sandbox |
| graph / persistence / telemetry / retrieval | 6/9/7/5 | 3.1k/2.4k/2.3k/2.2k | |

**Documentation drift (VERIFIED):**
- `CLAUDE.md` says 891 tests; reality is 1280 (1263 pass + 17 skip). `docs/HLD.md:84`, `docs/architecture.md:35`, `CLAUDE.md:19` say 12 nodes; code builds 13 (`agents/workflow.py:1632-1646`).
- `docs/AWS_HACKATHON_ARCHITECTURE.md` says "nothing has been applied"; a local `terraform.tfstate` (serial 137, 92 resources) shows it was.

---

## 2. Feature status

| Area | Status | Evidence |
|---|---|---|
| Alert ingest, dedup, queue, worker leases | **Complete** | `api/routers/alerts.py:55`, `persistence/jobs.py`, `tests/integration/test_job_leases.py` |
| LangGraph investigation workflow | **Complete, but no checkpointer** | see AI-1 |
| Evidence store, citation validation, confidence | Complete | `evidence/`, unit tests |
| Policy engine, tiers, kill switches | Complete | `policy/`, `execution/registry.py:54` |
| ActionGate / ValidatedAction / executors | Complete | `execution/validated.py` |
| Verification engine | Complete | `verification/` |
| Incident memory | Complete, contamination tests | `memory/` |
| Neo4j graph / GraphRAG | Complete; no Neo4j integration tests | CLAUDE.md known limitation |
| Hybrid retrieval, embeddings | Complete (Google embeddings) | `retrieval/` |
| GitHub / Slack integrations | **Unconfigured** (tokens empty) → degrades to evidence gap | CLAUDE.md |
| Horizon orchestrator (second runtime) | Partial docs; resume works | `agents/horizon/`, `test_horizon_resume.py` |
| Runtime adapter base | Interface stubs (9 `NotImplementedError`) | `integrations/runtime.py:309-355` — intentional, suppressed at `worker/main.py:522` |
| SQS queue (Terraform) | **Unused** | no SQS reference in `backend/src` |
| Frontend console (19 routes + war-room) | Mostly complete | see section 7 |
| Communication Center, Incident Calls, MCP Tool Explorer, Agent Eval page, provider-failure UX | **Missing** | UI spec §41, 42, 56, 52, 82; no backend routes either |
| Ask Aegis, incident memory page, environments, mobile incident view | **Partial** | spec §78-79, 49, 57, 68-69 |
| Fault injection (18 modes), benchmark | Complete | CLAUDE.md, `eval/` |
| Kind / Kubernetes workload | Empty placeholder | `infra/kind/` |

No feature found that is "mocked" in the production path; the workload topologies are intentionally synthetic shapes (documented).

---

## 3. Functional and API audit

- **Frontend↔backend contract (VERIFIED, static):** every path in `frontend/src/lib/{api,console-api}.ts` and `war-room/api.ts` maps to a backend route under `/v1`. Nav hrefs are a typed union, so dead links fail compile. Response shapes were not exercised.
- **Backend routes with no UI caller:** `POST /v1/alerts` (expected), `/health/ready`, `/metrics`, `/v1/policy/audit`.
- **End-to-end trace** alert→worker→graph→output is intact in code. It has not been re-run live in this audit (Docker daemon is down).
- **FUNC-1 (Medium, VERIFIED):** the docs promise worker-kill resume for the LangGraph path; the code cannot do it (AI-1).
- **FUNC-2 (Low, SUSPECTED):** `Makefile` declares `test-web` in `.PHONY`; its recipe was not confirmed.
- **FUNC-3 (Low, SUSPECTED):** possible dead/duplicated code: near-identical `_spec` helpers across `mcp/tools/{runtime,sandbox,topology}.py`; `agents/horizon/tools_self_edit.py` is referenced from only two files. Run `ecc:refactor-cleaner`/`knip`-style analysis before deleting.

---

## 4. Security findings

Strengths (VERIFIED): Firebase-token auth with role ladder VIEWER<RESPONDER<APPROVER<ADMIN taken from the token; every router except `alerts` (ingest token) and `health` requires a role; tool scopes fail closed (`api/deps.py:177`); `hmac.compare_digest` on the ingest token; production config validator refuses dev auth and wildcard CORS (`core/config.py:442-444`); parameterised SQL (the two f-string sites interpolate hard-coded column names only: `persistence/incidents.py:241`, `memory/store.py:382`); `UntrustedText` used in 17 modules; secrets are `SecretStr`; non-root containers.

| ID | Sev | Status | Finding | Fix | Verify |
|---|---|---|---|---|---|
| SEC-1 | **High** | VERIFIED (local) | Local `.env` holds real-looking long-lived **AWS IAM user keys** plus ~8 Google keys, LangSmith/OpenRouter/BFL/Nimble keys. Gitignored and never in history (`git log --all -- .env secrets.md` empty), but deployed design uses task roles. `secrets.md` also sits in repo root. | Rotate the AWS keys now; delete if unused; move to SSO/profile; keep `secrets.md` outside the repo. | `aws iam list-access-keys`; no key id in any file |
| SEC-2 | Med | SUSPECTED | `/v1/alerts` uses one shared token with no rate limit/replay protection: a leaked token lets anyone enqueue LLM runs (cost-DoS). | Per-source rate limit (ingest tier), token rotation, per-minute job cap. | test posting N alerts/min returns 429 |
| SEC-3 | Low | VERIFIED | Dev bypass token compared with `==` (`api/deps.py:~131`). Guarded by `auth_dev_mode and not is_production`. | `hmac.compare_digest`; confirm validator also rejects it for staging. | unit test on timing-safe path + staging config test |
| SEC-4 | Low | VERIFIED | `/metrics` unauthenticated (documented). Safe only if not internet-reachable; hackathon ALB is internal behind CloudFront, so likely fine. | Keep internal-only; assert in a Terraform test. | curl through CloudFront returns 403/404 |
| SEC-5 | Med | VERIFIED | `tfplan2`/`tfplan3` untracked and **not ignored** (`.gitignore` has `tfplan`, `*.tfplan`); plan files can embed secrets. `terraform.tfstate` is local, no remote backend. | Ignore `tfplan*`; delete both; move state to S3 backend (bootstrap env exists). | `git check-ignore infra/.../tfplan2` |
| SEC-6 | Low | VERIFIED | `infra/.../hackathon.tfvars` is tracked despite `*.tfvars` ignore (no credentials found; contains region, OIDC subject prefix, Firebase project id). | Decide intent; `git rm --cached` if it should be private. | `git ls-files` |
| SEC-7 | Low | VERIFIED | Frontend: no CSP header; auth token mirrored in `localStorage` (XSS-exposed). No `dangerouslySetInnerHTML` found. | Add CSP; prefer in-memory/httpOnly session. | header check; Lighthouse |
| SEC-8 | Low | VERIFIED | Base images tag-pinned not digest-pinned; compose mounts `docker.sock` into api/worker (dev-only, root-equivalent). | Pin digests; keep socket out of any deployed profile. | CI Trivy + manifest grep |
| SEC-9 | Low | SUSPECTED | `modules/iam/main.tf` has 8 `"*"` resources not individually reviewed; deploy role has `ecs:Register/DescribeTaskDefinition` on `*`. | `ecc:security-reviewer` pass on IAM. | IAM Access Analyzer |

**AI-specific security (VERIFIED unless noted):** model never sets tier/permission (static `policy/tiers.py`); single write tool requires `ValidatedAction`; kill switches admin-only; prompt-injection mitigation via `UntrustedText` envelopes (call-site completeness SUSPECTED). RAG poisoning: incident memory write path restricted to verified, approved outcomes (contamination tests exist). Gap to check: no adversarial eval scenario was seen for injection via log/commit text in the benchmark — confirm in `eval/scenarios/`.

---

## 5. AI, agents and RAG assessment

- **AI-1 (High, VERIFIED): no LangGraph checkpointer.** `agents/workflow.py:1934` is `graph.compile()`; the docstring (~1857), `docs/HLD.md:84,101`, `docs/LLD.md:440`, `docs/agents.md:190` all claim "Postgres checkpointer when available so a killed worker resumes". A grep for `checkpointer|PostgresSaver|MemorySaver` in `src` finds only the docstring. Killed workers restart the investigation (costing tokens, repeating tool calls). Resume exists only for Horizon. *Fix, simplest:* either wire `PostgresSaver` with thread_id = incident id (and test kill-resume), or state plainly that the main path is restart-safe-but-not-resumable and rely on idempotent nodes. Pick one; do not leave both claims.
- **AI-2 (Medium, VERIFIED): two agent runtimes.** LangGraph workflow plus `agents/horizon/` (own brain router, compactor, prompt version `horizon-1.0.0` vs `PROMPT_VERSION 1.1.0`). Separate prompt versioning and ambiguous ownership. Simplification candidate: document precisely which runs by default, or retire one after benchmark parity.
- **AI-3 (Low):** router has per-`TaskClass` models and API-key slot failover; Horizon falls back Bedrock→Gemini→scripted. Good. Token cost reporting per incident was not seen — SUSPECTED gap; check `telemetry/` and LangSmith cost fields.
- Retrieval: hybrid RRF with dimension check on embeddings; fails loudly rather than zero vectors (matches invariant). Neo4j down degrades confidence. `BudgetGuard` is orchestrator-owned. Prompt/confidence/policy versions exist; AI-behaviour changes require benchmark re-run (not run here).
- Simpler-architecture recommendation: keep the deterministic gate chain and graph; resist adding more agents. Wire the checkpointer or drop the claim; consolidate on one runtime.

---

## 6. Infrastructure and cost

**Verified deployed vs code-only.** Hackathon: local `terraform.tfstate` (terraform 1.9.8, 92 resources, last write 2026-09-26) shows an applied stack: RDS db.t4g.micro (private, encrypted), CloudFront with VPC origin, internal ALB, 7 ECS services, Secrets Manager, ECR, OIDC, budget. **Whether it is running today is NOT CHECKED** (no AWS calls made). `next.config.mjs` hardcodes `https://d2schpsj5u9197.cloudfront.net`. Staging and production are **code only** (no state present). No Vercel/Render/GCP infrastructure found beyond the CloudFront/Vercel origin variable and Firebase auth.

**Always-on cost drivers (hackathon, from state/code):** API Fargate on-demand 0.5 vCPU/1 GB; RDS t4g.micro 20 GB; ALB; CloudFront; public IPv4 per task; worker/obs/workload/loadgen on Spot. No NAT (`nat_strategy = none_public`), no EFS. Docs estimate ≈$60/mo, budget cap $100. Staging/prod (NAT, Multi-AZ, EFS, optional Redis) are $120-300/mo per project docs.

| ID | Sev | Finding | Recommendation |
|---|---|---|---|
| INF-1 | Med | SQS queue, DLQ, IAM and alarm provisioned in staging/prod but app uses Postgres queue; with `worker_min_count=0` nothing scales up. | Remove the `queue` module from staging/prod until a driver exists, or deploy `cpu` scaling + `min=1` as documented. |
| INF-2 | Med | Hackathon stack may be burning ~$2/day if left up. | If demo is done: `terraform destroy` after approval, or scale services to 0 (RDS stays billed). |
| INF-3 | Low | Spot worker interruption stalls an incident up to 15 min (acknowledged in tfvars). | Acceptable for demo; use on-demand worker before real use. |
| INF-4 | Low | Local state, no locking. | Use the bootstrap S3 backend. |
| INF-5 | Low | `ci-frontend` does not run vitest (92 tests) — see section 8. | Add `npm run test`. |

CI (9 workflows): backend, frontend, harness, security (secrets/deps/Trivy, weekly), terraform (fmt/tflint/validate/plan via OIDC), docker (ECR via OIDC), deploy-staging/production/hackathon. OIDC with no static keys is the right design; SEC-1 contradicts it locally.

---

## 7. Frontend and UX

**Stack:** Next 15.5, React 19, Tailwind 3.4 with CSS-variable tokens (black canvas, 4 surfaces, 5 status colours, 1 accent), Radix, TanStack Query, framer-motion, recharts, d3-force, cmdk, sonner. ~80 components; 19 console routes + war-room + marketing.

**Strengths:** coherent token system mirroring spec; shared `EmptyState`/`SourceUnavailableState`/`ErrorState`/skeletons used on every console page (keeps "no evidence" ≠ "source unavailable"); skip link, `:focus-visible`, reduced-motion, ~297 aria/role attributes.

| ID | Sev | Finding | Fix |
|---|---|---|---|
| UX-1 | Med | Thin responsiveness (~43 breakpoint classes); spec §68-69 mobile incident view absent; dense tables and force graph likely break < 768px. | Responsive pass on incidents, systems, approvals; mobile incident view. |
| UX-2 | Med | Tertiary text `#666` on black ≈3.7:1 (fails AA body); 0.68rem meta text. | Lift to ≥4.5:1 (#8a8a8a-ish), min 12px. |
| UX-3 | Med | Missing screens (Communication Center, Calls, MCP Tool Explorer, Agent Eval, provider-failure UX). | Decide scope: ship MCP Tool Explorer (cheap, read-only) first; drop Calls unless needed. |
| UX-4 | Low | No automated a11y or visual tests; no `<main>`/landmark verification. | axe via Playwright on key routes. |
| UX-5 | Low | No CSP (SEC-7). | |

**Versus Linear/Vercel/Claude:** the dark token base and state components are on the right path; missing are consistent type scale, density modes, keyboard-first navigation beyond cmdk, motion polish and light mode. Use one design skill (`impeccable` or `frontend-design`, not both) to generate a short `DESIGN.md` and enforce it; do not add a third UI kit.

---

## 8. Testing and production readiness

- **Backend:** strong. 1263 pass; strict markers; safety-property tests (gate, policy, MCP invoker, contamination). Integration suite is Postgres-only and skipped without a DB (17 skips).
- **TEST-1 (Med, SUSPECTED):** no unit tests found referencing auth dependencies (`require_role`, `current_principal`); only `test_api_evaluation.py` and `test_war_room_api.py` touch router auth. Add a matrix test: each router × role → 401/403/200, including that `alerts` rejects a bad token.
- **TEST-2 (Med, VERIFIED):** no Playwright/e2e; frontend has 9 vitest files, none for console pages. No test exercises UI→API→DB.
- **TEST-3 (Med, VERIFIED):** `ci-frontend` runs lint/typecheck/build but not vitest.
- **TEST-4 (Low):** no Neo4j/Redis integration suite; no worker-kill-mid-graph test for the LangGraph path (would fail today because of AI-1).
- **Readiness blockers before real use:** rotate keys (SEC-1); resolve checkpointer claim (AI-1); remote Terraform state (INF-4); decide SQS (INF-1); auth matrix test (TEST-1); confirm deployed health.
- **Recommended journeys (Playwright):** (1) sign-in → overview → incident list → incident detail with evidence; (2) seed alert → incident appears via SSE → diagnosis → approval request → approve → verified; (3) kill switch toggle blocks execution; (4) source-unavailable state renders distinctly from empty.

---

## 9. Claude Code plugins and configuration

**Current (VERIFIED):** 12 global plugins (ecc, agent-skills, claude-mem, caveman, superpowers, impeccable, frontend-design, figma, ponytail, claude-code-setup, security-guidance, claude-security), ~330 skills, plus many claude.ai connectors (Vercel, Notion, Gmail, Zapier, Unsplash, Drive, Calendar…). Static context cost is roughly 20-30k tokens per session before any work.

**Problems:**
- **CFG-1 (Med, VERIFIED): graphify MCP fails (`CONNECTION_CLOSED`).** Two configs with different flags: root `.mcp.json` uses `--graph`, untracked `.claude/.mcp.json` uses `--graph-path`. Keep one (the root, tracked) and verify the flag with `graphify-mcp --help`; delete the other.
- **CFG-2 (Med):** heavy overlap — review/TDD/planning/debugging/git exist in ecc, agent-skills, superpowers, engineering; `frontend-design` ≈ `impeccable` ≈ `ecc:frontend-*`; `claude-security` ≈ `security-guidance` ≈ `ecc:security-*`; memory in claude-mem ≈ ecc learn.
- **CFG-3 (Low):** `AGENTS.md` (148 lines) and `CLAUDE.md` (270) likely duplicate rules; `.codex/config.toml` also present.
- **CFG-4 (Low):** unused connectors add tool-name and instruction tokens every session.

**Recommendation (needs your approval; nothing installed or removed):**
- Keep: `graphify` (navigation; the repo already depends on it), one workflow bundle (suggest `ecc` since CLAUDE.md names its agents), `impeccable` for frontend, `claude-security` for periodic scans, `claude-mem` only if you actively use it.
- Disable per-project (`enabledPlugins` in `.claude/settings.json`): `agent-skills`, `superpowers`, `figma`, `ponytail`, `frontend-design` unless used. `caveman` only if you want terse output (it's active now).
- Playwright: add the official Playwright MCP / `@playwright/test` as a devDependency, not another plugin; the repo already has `ecc:e2e-runner`.
- Ponytail, Caveman and Graphify were checked only for presence/enable state in config; I did **not** do web verification of upstream versions or conflicts in this audit (NOT CHECKED). Do that in a short follow-up before installing anything new.
- Remove unused claude.ai connectors in the account settings; they cost tokens in every session.

---

## 10. Context-management and context-rot strategy

1. **Three-tier instructions.** `CLAUDE.md` (≤150 lines): invariants, commands, never-do list only. Move the directory table, deployment and known-limitations prose to `docs/` and link. Delete `AGENTS.md` or make it a 5-line pointer to `CLAUDE.md` (single source of truth).
2. **Kill stale numbers.** Replace "891 tests", "12 nodes", "14 services" with commands that print the truth (`make status`) or remove them. Stale facts are the main source of confidently wrong sessions (this audit already found three).
3. **Progressive disclosure.** Use `graphify` first, then read only the files it names; keep specs (`PRD.md` etc., 7k lines) out of context unless a task cites a section. Add a `docs/INDEX.md` of one line per doc.
4. **Subagents for isolation.** Reviews, scans and log-heavy work go to subagents returning ≤300-word findings with file:line.
5. **Checkpoints.** One `docs/STATUS.md` (≤40 lines: current goal, decisions, next 3 steps) updated at task end; `claude-mem` handoff or `/compact` at ~60% context; start a fresh session per task.
6. **Decision log.** Keep ADRs in `docs/adr/` (one file per decision, e.g. "checkpointer: wire vs drop"), so sessions don't re-litigate.
7. **Gate hygiene.** GateGuard is on and demands facts per new file; keep it, but add `GATEGUARD_EXEMPT_GLOBS` for `docs/**` and scratch paths to avoid repeated cost on documentation edits.
8. **Plugin budget.** Target ≤ 6 plugins / ≤ 100 skills enabled per project.

---

## 11. Git, worktree, RAM, disk and workspace cleanup plan

**Inventory (VERIFIED, read-only):**

| Item | State |
|---|---|
| Worktrees | **1** (`C:/dev/aegis-2.0`, `main`, `579e461`). No `.claude/worktrees`. Nothing to remove. |
| Local branches | `main` (in sync with `origin/main`), `pre-reset-main` |
| `pre-reset-main` | 26 commits, **no merge-base with main** (history was rebuilt), pushed to `legacy/main`. **Manual review; do not delete.** |
| Stashes | none |
| Untracked | `.claude/.mcp.json`, `infra/terraform/environments/hackathon/tfplan2`, `tfplan3` |
| Remote refs | ~19 `origin/dependabot/*` (open PRs, decide), ~18 `legacy/dependabot/*` (old repo, prunable locally) |
| `.git` | 8.5 MB, 1528 loose objects, 0 packs (`git gc` optional) |
| Remotes | `origin` = udarshmarthala/aegis-hackathon, `legacy` = udarshmarthala/aegis-ai-sre. `git fetch` was not run, so remote state may be stale. |

**Disk (~8.1 GB, all gitignored/regenerable):** 10 × `.terraform` provider caches ≈ **6.9 GB** (686 MB each: bootstrap, hackathon, staging, production, and `modules/{compute,datastores,graph,iam,network,observability}`), `frontend/node_modules` 610 MB, `frontend/.next` 277 MB, `backend/.venv` 233 MB, `graphify-out` 24 MB. Outside the repo: `~/.claude/projects` holds `pickready` 1.37 GB, `OpenFrame` 514 MB, this project 122 MB, plus ~10 stale agent-worktree project dirs.

**RAM vs disk.** Deleting files frees disk only. RAM is held by running processes: 7 `claude` ≈ 1.9 GB, 14 `node` ≈ 813 MB, postgres 8 ≈ 19 MB. Docker daemon is not running. Closing idle Claude sessions and stopping dev servers frees RAM; removing `.terraform` or worktrees does not.

**Proposed cleanup (NOT executed; needs your approval; all reversible by re-running `terraform init`):**

```powershell
# 1. Safe: module dirs never need init (roots do the init)
foreach ($m in 'compute','datastores','graph','iam','network','observability') {
  Remove-Item -Recurse -Confirm "infra/terraform/modules/$m/.terraform"
}
# 2. Stop duplicate provider downloads in future
setx TF_PLUGIN_CACHE_DIR "$env:USERPROFILE\.terraform.d\plugin-cache"
# 3. Plan files (binary, may hold secrets) - verify then delete
Remove-Item infra/terraform/environments/hackathon/tfplan2, infra/terraform/environments/hackathon/tfplan3
# 4. Ignore rule: add `tfplan*` to .gitignore
# 5. Local-only remote-ref prune (touches no remote)
git remote prune legacy      # review `git remote prune --dry-run legacy` first
# 6. Optional later: remove environment .terraform for envs you don't use
```

Do **not** touch: `main`, `pre-reset-main`, `origin/dependabot/*` (decide per PR), `terraform.tfstate*`, `.env`, `secrets.md`.

**Permanent `CLAUDE.md` rules to add:**
- Never create a git worktree unless asked; at most one extra, removed with `git worktree remove` once merged and pushed.
- Terraform plans go to `$TMP`/scratchpad, never the repo; run `terraform init` only in environment roots; `TF_PLUGIN_CACHE_DIR` is set.
- Close sessions/dev servers when done; one active Claude session per repo.
- Before any branch deletion: `git branch --merged main`, `git log main..<b>`, confirm upstream. Never force-remove worktrees.
- Generated output (`.next`, `.venv`, `graphify-out`) is never committed or hand-edited.

---

## 12. Prioritised roadmap

**P0 — this week (blockers / risk)**
1. Rotate and remove local AWS IAM keys; move `secrets.md` out of the repo (SEC-1).
2. Delete `tfplan2/3`, ignore `tfplan*` (SEC-5); decide whether to keep the hackathon stack running (INF-2).
3. Resolve the checkpointer: wire `PostgresSaver` + kill-resume test, or correct four docs (AI-1).
4. Fix graphify MCP: one `.mcp.json`, correct flag (CFG-1).

**P1 — next 1-2 weeks (correctness / confidence)**
5. Auth matrix test across all routers; `compare_digest` fix; alert rate limit (TEST-1, SEC-2, SEC-3).
6. Add vitest to `ci-frontend`; add Playwright with the four journeys (TEST-2/3).
7. Refresh `CLAUDE.md`: remove stale counts, shorten to ≤150 lines, retire `AGENTS.md` duplication; add hygiene rules (sections 10-11).
8. Remote Terraform state; decide SQS removal vs driver (INF-1, INF-4).
9. Run `eval/run.py --suite smoke` and record baseline before any prompt change.

**P2 — 2-4 weeks (product completeness / UX)**
10. Responsive pass + contrast fixes + CSP (UX-1, UX-2, SEC-7).
11. MCP Tool Explorer and provider-failure UX; decide fate of Communication Center and Calls (UX-3).
12. Consolidate agent runtimes or document Horizon vs LangGraph ownership (AI-2); add per-incident token cost to the UI/telemetry.
13. Plugin diet to ≤6 plugins; remove unused connectors (CFG-2/4).

**P3 — later**
14. Neo4j/Redis integration tests; digest-pinned images; IAM wildcard review (SEC-8/9).
15. Real-world pilot checks: GitHub/Slack credentials, on-demand worker, Multi-AZ RDS, staging deploy.

**Skeptical notes.** Strong test counts hide that nothing exercises the UI end-to-end. Several docs assert behaviours the code lacks (checkpointer). The most expensive risk is not architecture but a leaked long-lived key, so P0 is hygiene, not features. Do not add more agents, plugins or infra before P0-P1 are done.

---
*Awaiting your approval before any implementation or cleanup.*
