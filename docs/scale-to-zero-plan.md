# Aegis — six-phase product and scale-to-zero plan

**Status:** Phases 1–3 complete. Phases 1–6 are build-and-test phases. AWS
deployment is a separate, explicitly approved gate after all six phases.

**Infrastructure goal:** zero idle application compute, pay-per-use execution,
minimal recurring charges, and Terraform-managed AWS infrastructure.

**Product goal:** make the incident lifecycle understandable and navigable for
software engineers without hiding SRE depth, weakening controls, or presenting
simulated or inferred information as observed fact.

This plan integrates the product-experience and cinematic-site brief into the
existing scale-to-zero dependency order. The root `PRD.md`, `ESD.md`,
`AIArchitecture.md`, and `Aegis_UIUX_Spec.md` remain authoritative. Conflicts
are recorded as proposed revisions, not silently applied to those documents.

## Progress

| Phase | Infrastructure scope | Product scope | Branch | Status |
|---|---|---|---|---|
| 1 | Durable dispatch and bounded worker | Actual-state workflow audit and product-state contract | `scale-zero/p1-dispatch-worker` | complete |
| 2 | API on Lambda | Secure entry, auth completion, sample-mode contracts | `scale-zero/p1-dispatch-worker` | complete |
| 3 | Terraform foundation | Goal-oriented shell, responsive navigation, first-run exploration | `scale-zero/p1-dispatch-worker` | complete |
| 4 | Orchestration and compute | Incident-to-result operational workflows | `scale-zero/p4-tf-orchestration` | not started |
| 5 | Frontend polling and local E2E | Service graph and cinematic landing experience | `scale-zero/p5-local-e2e` | not started |
| 6 | Pre-deploy hardening and docs | Route closure, usability, accessibility, security and performance | `scale-zero/p6-predeploy` | not started |
| Deploy gate | Apply, validate, cut over, deprecate | Production journey validation | none | blocked on phases 1–6 and approval |

Update this table in the same commit that finishes a phase.

## Architecture target

```text
Browser → Next.js (Vercel) → API Gateway → Lambda → Aurora PostgreSQL Serverless

incident + workflow job + dispatch outbox (one transaction)
                      ↓
                 Step Functions
                      ↓
         ECS Fargate RunTask → bounded worker → checkpoint/result → Postgres/S3
```

- PostgreSQL remains the system of record.
- Neo4j remains a topology projection and cannot become authoritative.
- Redis remains non-authoritative and optional.
- No NAT Gateway, continuously running ALB, provisioned Lambda concurrency, or
  always-on ECS worker is introduced in the scale-zero environment.
- The existing Postgres-polling worker remains the default until the new path is
  explicitly configured and validated.

## Product-state contract

Every user-facing datum or result must be labelled by its real state:

| State | Meaning | UI rule |
|---|---|---|
| Observed | Directly measured or persisted operational fact | Show source, time and provenance where available |
| Derived | Deterministic computation from observed inputs | Explain the derivation and link inputs |
| Inferred | Hypothesis or model conclusion with validated evidence references | Show confidence, evidence and uncertainty; never style as proven |
| Sample | Deterministic isolated exploration data | Persistent “Sample environment” label; never mix with a live workspace |
| Unavailable | Source could not be consulted | Show the unavailable source and reason |
| No result | Source was consulted and returned no matching evidence | Never collapse into “Unavailable” |

The product audit and current gaps are maintained in
[`docs/product-experience-audit.md`](product-experience-audit.md).

## Delivery loop for every phase

1. Create a short-lived worktree from current `main`.
2. Build only the phase’s scoped vertical slice.
3. Run relevant gates:
   - Backend: `pytest tests -q`, `ruff check src tests`, `mypy src`.
   - Frontend: `npm run test`, `npm run build`, `npm run lint`,
     `npm run typecheck`.
   - Terraform: `terraform fmt -check`, `terraform init -backend=false`,
     `terraform validate`, tflint and the repository security scan. Never apply.
   - Write path, policy, evidence or tool boundary: `eval/run.py --suite smoke`.
4. Route the diff to the matching Python, database, security, silent-failure,
   React and accessibility reviewers. Resolve findings or record them.
5. Merge to `main`, push, confirm remote state and CI.
6. Remove the phase worktree and merged branch only when clean and recoverable.

A green happy path is insufficient. New safety behavior requires a regression
test that fails when the property is removed.

## Phase 1 — reliable execution and product truth

**Infrastructure scope (backend only, no AWS):**

- Add a forward-only transactional dispatch outbox for every `workflow_jobs`
  insertion, including idempotent backfill for active jobs.
- Add a dispatcher port with the current Postgres path as the default and an
  opt-in Step Functions Standard adapter.
- Use the job id as the stable external idempotency key. Bound boto3 socket and
  caller timeouts, retry with jitter only because `StartExecution` is
  idempotent by execution name, and protect it with a circuit breaker.
- Recover dispatch rows whose owner died before settlement.
- Add lease fencing to job claims so a stale worker cannot settle a job after a
  replacement owns it.
- Add `--once`, `--max-jobs`, and `--max-seconds` worker modes. Stop claiming at
  the bound, drain safely, checkpoint, release work and exit.
- Restore Horizon budget *usage* from checkpoints while deriving all maxima
  only from frozen settings. Check the budget before every step.

**Product scope (documentation and contracts only):**

- Inventory current routes, navigation, APIs, auth, evidence, incident,
  service, graph, evaluation and war-room workflows from the implementation.
- Record implemented, partial, missing, simulated and unavailable capabilities.
- Define the product-state contract above and later-phase dependencies.
- Record spec conflicts and trace the new brief to phases 2–6.

**Required regression evidence:**

- incident/job/outbox commit or roll back together;
- duplicate enqueue produces one live job and one dispatch request;
- duplicate Step Functions dispatch maps to the same execution identity;
- stale dispatch is recovered;
- a stale job lease cannot complete or fail a replacement lease;
- checkpoint survives worker death and the job is reclaimable;
- bounded worker exits with no work and cannot accept budget-raising CLI input;
- malicious checkpoint maxima cannot raise configured agent budgets.

**Out of scope:** AWS resources, Lambda, frontend redesign, public auth changes,
sample data creation, graph replacement, and cinematic motion.

**Reviewers:** Python, database, silent-failure, architect. Eval smoke required.

**Acceptance recorded:** transactional outbox, job and dispatch lease fencing,
bounded lifecycle controls, frozen budget authority and recovery behavior are
covered by unit and live-Postgres integration tests. Ruff, mypy and the full
backend suite pass. The smoke evaluator completed safely; workload-dependent
scenarios were classified as harness failures because the reference workloads
were not running, while the executed false-positive scenario abstained and
performed no unsafe action.

## Phase 2 — portable API and secure entry

**Infrastructure:**

- Add a Mangum entry point around the existing FastAPI app with identical auth
  and router contracts.
- Move migrations out of Lambda startup into a one-off migration command.
- Add authenticated polling that preserves monotonic event ids and
  `Last-Event-ID` semantics; keep SSE for local/ECS during transition.
- Use native asyncpg in the VPC unless a measured spike proves the Data API can
  preserve transactions, row locks and repository contracts.
- Return a typed `database resuming` response with `Retry-After`; never return
  an empty success for an unavailable database.

**Product:**

- Complete Firebase email/password, Google, sign-up, sign-in, reset and session
  recovery flows.
- Remove the public development-token form. Retire the backend bypass only with
  a secure emulator/test-auth replacement.
- Specify tenant/environment isolation and deterministic sample-mode API
  contracts. New accounts receive no Aegis role until an administrator grants
  a tenant- and environment-scoped role.

Polling is a proposed transport revision to ESD §21. Event ordering and replay
semantics remain unchanged until the authoritative spec is revised.

**Acceptance recorded:** the Mangum handler imports with Lambda-safe settings;
migrations have a separate command and are prohibited during Lambda startup;
database wake-up has a typed 503/`Retry-After` contract; replay polling is
authenticated, bounded and monotonic; Firebase sign-in, sign-up, reset, token
recovery and emulator development are implemented with memory-only bearer
tokens; the public token bypass is removed; tenant and environment scope is
enforced across incident-derived APIs; server-side sample contracts fail
closed; and the production dependency audit is clean.

## Phase 3 — scale-zero foundation and first-run shell

**Infrastructure (plan/validate only):**

- VPC and restrictive security groups with no NAT Gateway or unnecessary
  inbound access.
- Least-privilege IAM and GitHub Actions OIDC.
- Aurora PostgreSQL Serverless v2 with zero-ACU minimum where supported,
  bounded maximum capacity, one writer and no RDS Proxy when pause is required.
- Private S3 artifacts with blocked public access and finite lifecycle.
- Secrets in SSM Parameter Store or Secrets Manager.
- Decide, with cost evidence, public-IP worker egress versus VPC endpoints.

**Product:**

- Reorganize the shell around incident response goals without hiding useful
  capabilities.
- Add responsive mobile navigation, breadcrumbs/context links and shared
  loading/empty/error/permission language.
- Implement clearly labelled, isolated first-run exploration using the
  deterministic reference scenarios already defined by the PRD.

**Acceptance recorded:** the dedicated `scale-zero-foundation` Terraform root
validates and passes its native contract test. It has no NAT Gateway or
hourly-billed interface endpoints, uses one zero-ACU-capable Aurora writer,
customer-managed encryption, private finite-lifecycle artifacts, empty secret
containers, default-deny networking and scoped OIDC/runtime roles. Checkov
passes with explicit reviewed deferrals for Phase 4 attachment, logging,
rotation and regional-resilience resources. The responsive shell, accessible
mobile navigation, product breadcrumbs, shared permission language and
server-backed isolated `/explore` journey pass lint, TypeScript, unit tests and
the production build.

## Phase 4 — on-demand orchestration and core operational UX

**Infrastructure (plan/validate only):**

- API Gateway HTTP API and Lambda with throttling and access logs.
- ECS task definition with no ECS service; tasks exist only for `RunTask`.
- Replace the foundation's temporary public HTTPS worker egress with an
  allowlisted egress control or explicitly approved destinations before any
  deployment can pass its gate.
- Step Functions Standard with `RunTask.sync`, bounded retries, catch, timeout
  and concurrency.
- EventBridge recovery sweep, finite CloudWatch retention, stuck-outbox and
  execution alarms, AWS Budgets and bounded AI spend.

**Product:**

- Connect incident, investigation, evidence, recommendations, approvals,
  execution, verification and results as one navigable workflow.
- Put contextual actions beside the evidence that justifies them.
- Preserve authorization, policy, leases and verification end to end. No dead
  controls or frontend-only success messages.

## Phase 5 — integrated frontend, service graph and cinematic landing

**Infrastructure/application integration:**

- Move the frontend event client to authenticated polling without changing
  visible incident behavior.
- Run Lambda handler → outbox dispatcher → bounded worker → local Postgres end
  to end.
- Drill worker kill, duplicate dispatch, dispatcher restart and lease expiry.

**Service graph:**

- Benchmark the existing bounded SVG graph before selecting React Flow,
  Cytoscape, Sigma or ELK. Use one visualization architecture.
- Add directional dependencies, observed/unknown/unavailable health,
  search/filter/focus, contextual service details, related incidents, evidence,
  telemetry and navigation.
- Visually distinguish observed facts, inferred relationships and hypotheses.
  Never invent topology or health.

**Landing experience:**

- Build one art-directed Three.js-led scene around “Your application broke.
  Aegis finds out why.” and the stability → anomaly → investigation → recovery
  progression.
- Use GSAP only for coordinated timelines and ordinary motion only for
  interface transitions; do not let systems fight over the same properties.
- Keep secure authentication readable on the right at desktop and adapt it for
  mobile.
- Provide reduced-motion and non-WebGL experiences, adaptive DPR/complexity,
  lazy loading, cleanup, hidden-tab suspension and measured performance.

The cinematic landing is deliberately Phase 5: the authoritative UI spec
prioritizes operational product surfaces over marketing work.

## Phase 6 — whole-product hardening and delivery

- Close every route and gap against the Phase 1 inventory.
- Run Playwright user journeys, route/component tests, automated accessibility,
  responsive checks at 320/768/1024/1440, security regression, performance
  budgets and representative graph-size tests.
- Add a `workflow_dispatch`-only scale-zero deployment workflow with separate
  plan/apply jobs and a reviewed GitHub Environment. Destructive plans stop.
- Perform a read-only target-account plan and cost estimate.
- Write deploy, cutover, rollback and Aurora-pause runbooks.
- Complete cross-phase security and architecture review and the ESD §44
  Definition of Done evidence.
- Draft deprecation notices for separately authored AWS documents; do not edit
  those documents without explicit approval.

## Deployment gate — explicit approval required

Deployment starts only after phases 1–6 are merged, pushed and green.

1. Review plan, approve, apply, run migrations separately and smoke test.
2. Validate cold start/database resume, prolonged-idle investigation,
   interruption/retry/idempotency, auth/audit/policy, task termination, actual
   Aurora pause, alerting, retention and bounded AI use.
3. Validate the complete product journey with live state labels.
4. Cut Vercel to API Gateway and retain the old stack for a rollback window.
5. Deprecate the old hackathon stack only after the window closes.

Never destroy the old database before a backup has been restored successfully.
Production remains outside this replacement unless separately approved.

## Brief-to-phase traceability

| Overhaul brief | Delivery phase |
|---|---|
| Understand existing product and map workflows | 1 |
| Workflow language, contextual actions and action/result continuity | 3–4 |
| Isolated exploration and onboarding | 2–3 |
| Professional incident-aware service graph | 5 |
| Route-by-route product redesign | 3–4, closure in 6 |
| Cinematic full-scale landing and right-side auth | 2 auth, 5 visuals |
| End-to-end backend integration | every phase, full drill in 5 |
| Real user journeys, accessibility, security and performance | incremental, release gate in 6 |
| Delivery report and accurate documentation | every phase, consolidated in 6 |

## Deprecation order

The scale-zero environment replaces the hackathon ECS service, ALB and RDS
deployment only after live validation:

1. cut over;
2. keep a rollback window;
3. mark the old hackathon environment and workflow deprecated;
4. restore-test a backup;
5. obtain separate destructive approval;
6. destroy and later remove modules only when no environment uses them.

The staging environment is decided after a full validation cycle. Production is
kept unless a separate plan explicitly replaces it.
