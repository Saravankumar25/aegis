# Aegis — AWS scale-to-zero infrastructure plan

**Status:** proposed, not started. Phases 1–6 build and test only. Nothing is created in AWS until phase 7, and phase 7 needs explicit approval.

**Goal:** zero idle compute cost, pay-per-use execution, minimal recurring charges, Terraform-managed AWS infrastructure.

## Progress

| Phase | Scope | Branch | Status |
|---|---|---|---|
| 1 | Durable dispatch and bounded worker (backend) | `scale-zero/p1-dispatch-worker` | not started |
| 2 | API on Lambda (backend) | `scale-zero/p2-lambda-api` | not started |
| 3 | Terraform foundation | `scale-zero/p3-tf-foundation` | not started |
| 4 | Terraform orchestration and compute | `scale-zero/p4-tf-orchestration` | not started |
| 5 | Frontend polling and local end-to-end | `scale-zero/p5-local-e2e` | not started |
| 6 | Pre-deploy hardening, docs, deploy workflow | `scale-zero/p6-predeploy` | not started |
| 7 | Deploy to AWS, validate, cut over, deprecate | none (deploy gate) | blocked on 1–6 |

Update this table in the same commit that finishes each phase.

## 1. Existing architecture

Aegis is an AI-powered incident response platform using FastAPI, Next.js, PostgreSQL, LangGraph, Neo4j, hybrid retrieval and AI agents.

Its current AWS infrastructure uses ECS Fargate, RDS PostgreSQL, CloudFront, an Application Load Balancer and Terraform. The existing hackathon deployment is estimated at roughly $60/month when running, and still incurs charges when compute is stopped.

**Objective:** eliminate continuously running application containers, load balancers, NAT Gateways and provisioned database compute while preserving existing functionality and security guarantees.

## 2. Target architecture

```
Browser → Next.js (Vercel) → API Gateway → AWS Lambda → Aurora PostgreSQL Serverless

Incident created → durable job record (outbox) → Step Functions → ECS Fargate RunTask
                 → LangGraph investigation → PostgreSQL / S3 → frontend
```

### Frontend

- Keep Next.js on Vercel.
- Avoid unnecessary frontend migration.
- Preserve authentication and existing UI functionality.

### Backend API

- Deploy FastAPI on AWS Lambda using Mangum.
- Use an API Gateway HTTP API for HTTPS requests.
- Configure Lambda with zero provisioned concurrency.
- Run database migrations outside Lambda startup.
- Replace long-lived Server-Sent Events with authenticated polling, or a separately designed real-time service.
- Preserve API contracts wherever practical.

### AI investigation worker

- Replace the continuously running ECS worker service with on-demand ECS Fargate `RunTask`.
- Use Step Functions Standard to coordinate investigations.
- Add a bounded worker execution mode that processes assigned jobs and exits.
- Preserve LangGraph checkpoints, idempotency, PostgreSQL leases, retries and evidence validation.
- Set CPU and memory from benchmarks.
- Stop compute automatically when investigations finish.
- Set maximum concurrent tasks and per-investigation budgets.

## 3. Database and storage

### PostgreSQL

- Use Aurora PostgreSQL Serverless v2 with a zero-ACU minimum, where supported.
- Pause automatically after roughly 5–10 minutes of inactivity.
- Use one writer instance with capacity limits.
- Keep PostgreSQL as the system of record.
- Preserve transactions, row locking and the existing schema.
- Adapt Lambda database access for the Aurora Data API or appropriate VPC connectivity (decided in phase 2).
- Keep native PostgreSQL connectivity for workers where required.
- Do not use RDS Proxy if automatic pause is mandatory.

### Neo4j and Redis

- Keep Neo4j optional where degraded operation is already supported.
- Do not provision always-on Neo4j or Redis containers for the initial deployment.
- Use the existing application fallback behaviour where valid.

### S3

- Store investigation artefacts, reports and exports.
- Enable lifecycle expiration and private bucket access.
- Avoid unnecessary always-on storage infrastructure.

## 4. Networking and security

- Avoid NAT Gateways and continuously running ALBs.
- Use API Gateway for public API ingress.
- Allow Fargate workers controlled outbound connectivity.
- Apply restrictive security groups with no unnecessary inbound access.
- Use IAM roles instead of static AWS credentials.
- Keep GitHub Actions OIDC authentication.
- Store secrets in SSM Parameter Store or Secrets Manager.
- Retain deterministic approval gates and disable autonomous production remediation until revalidated.

## 5. How every phase is delivered

Each of phases 1–6 follows the same loop. A phase is not done until the last step.

1. **Worktree.** Create a worktree outside the repo from current `main`:
   `git worktree add ../aegis-sz-pN -b scale-zero/pN-<name> main`.
2. **Build** inside the worktree only.
3. **Test gates**, all green:
   - Backend: `pytest tests -q`, `ruff check src tests`, `mypy src`.
   - Frontend, when touched: `npm run test`, `npm run build`, `npm run lint`, `npm run typecheck`.
   - Terraform, when touched: `terraform fmt -check`, `terraform init -backend=false`, `terraform validate`, tflint, and the repo's security scan. No `apply`.
   - Phase touched the write path, policy, evidence or the tool boundary: `eval/run.py --suite smoke`, exit code and unsafe-scenario section checked.
   - New safety behaviour has a test that fails if the property is removed, not only a happy path.
4. **Review** by the matching agent from the `CLAUDE.md` table (`ecc:python-reviewer`, `ecc:database-reviewer`, `ecc:security-reviewer`, `ecc:silent-failure-hunter`, `ecc:react-reviewer` and so on). Findings fixed or recorded, not self-certified.
5. **Merge** to `main`, then **push**.
6. **Clean up.** `git worktree remove ../aegis-sz-pN` and `git branch -d scale-zero/pN-<name>`. If any gate failed, the worktree stays and the phase stays open.
7. Update the progress table.

### Pushing to `main` has side effects

A push to `main` runs `docker`, and a successful `docker` run triggers `deploy-staging` (skipped only when `AWS_DEPLOY_ROLE_ARN` is not configured). `ci-terraform` also runs on any `infra/terraform/**` change. Therefore:

- Every backend change in phases 1–2 must stay backward compatible with the existing ECS stack. Old behaviour stays the default; new behaviour is opt-in by configuration.
- New migrations are forward-only and idempotent, and must apply cleanly to an existing staging database. An applied migration is never edited.
- The `scale-zero` environment has no apply path in CI until phase 6, and that path is `workflow_dispatch` only, behind a GitHub Environment with reviewers. `ci-terraform` never applies.
- Before the first push, confirm whether the staging deploy role is configured. If it is, phase 1 and 2 pushes will deploy to staging.

## Phase 1 — Durable dispatch and bounded worker

Backend only. No AWS.

- **Outbox.** New forward-only migration (next free number). An outbox row is written in the same transaction as the incident and its `workflow_jobs` row, so an incident cannot be lost between commit and workflow start.
- **Dispatcher.** Drains the outbox idempotently behind a port. Two adapters: the current Postgres-polling path (default) and a Step Functions `StartExecution` adapter (boto3 is already a dependency). A recovery sweep re-dispatches rows that were committed but never started.
- **Bounded worker mode.** A `--once` style mode that claims up to N jobs or a time budget, then exits cleanly. Existing `FOR UPDATE SKIP LOCKED` claiming, leases, checkpoints, retries and graceful shutdown are unchanged.
- **Tests that must fail if the property is removed:** outbox row survives a crash between commit and dispatch; duplicate dispatch runs the job once; worker killed mid-job is reclaimed after lease expiry and resumes from its checkpoint; bounded worker exits with no work; budget limits are not raisable by the worker.
- **Reviewers:** python, database, silent-failure. Eval smoke required.

## Phase 2 — API on Lambda

Backend only. No AWS.

- **Handler.** Mangum entry point around the existing FastAPI app. Same routers, same auth boundary, same API contracts.
- **Migrations out of startup.** A one-off migrate command; the Lambda never migrates. Staging already has a migrate task to reuse.
- **SSE replacement.** An authenticated polling endpoint that keeps the existing event ids and `Last-Event-ID` semantics. SSE stays for local and ECS.
- **Database access decision.** Recommendation: Lambda in the VPC with native `asyncpg`, a pool of 1–2, statement timeouts on every transaction. The Data API would mean replacing `asyncpg` across the persistence layer and re-proving transaction and row-locking semantics. Confirm with a short spike before building; record the decision in this file.
- **Database resuming.** A paused Aurora takes time to wake. The API returns a typed "database resuming" response with `Retry-After`, never a hang and never an empty result. This keeps "no evidence found" and "source unavailable" distinct (invariant 6) and fails closed (invariant 5).
- **Tests:** handler runs with zero infrastructure; contract tests show unchanged responses; a resuming database yields the typed error, not a 200 with empty data; unauthenticated requests are rejected through the Lambda path.
- **Reviewers:** python, fastapi, security.

## Phase 3 — Terraform foundation

New `infra/terraform/environments/scale-zero/` and modules. `plan` and `validate` only.

1. Networking and security groups. No NAT Gateway, no ALB, no inbound rules beyond what is needed.
2. IAM roles and permissions, least privilege, OIDC for GitHub Actions.
3. Aurora PostgreSQL Serverless v2: `min_capacity = 0`, auto-pause 5–10 minutes, one writer, capped max capacity, no RDS Proxy. Pin an AWS provider version that supports zero-ACU.
4. S3 artefact bucket: private, public access blocked, lifecycle expiry.
5. Secrets in SSM Parameter Store or Secrets Manager.

**Open design point:** a Lambda in the VPC with no NAT cannot reach the internet, and interface VPC endpoints bill hourly, which works against zero idle cost. Decide here between keeping the API Lambda's outbound needs to Aurora and S3 only (gateway endpoint, free) or budgeting specific interface endpoints. Workers need outbound access for Bedrock, GitHub and Slack; decide public-subnet Fargate with a public IP and tight security groups versus endpoints.

Reviewers: security, database. Existing environments are untouched.

## Phase 4 — Terraform orchestration and compute

Same environment. `plan` and `validate` only.

1. API Gateway HTTP API and Lambda: zero provisioned concurrency, throttling, access logs.
2. ECS cluster and worker task definition. **No ECS service**; tasks exist only when `RunTask` starts them. CPU and memory set from a benchmark, with the figure recorded.
3. Step Functions Standard state machine: `RunTask.sync`, retry and catch, execution timeout, maximum concurrency.
4. Scheduled recovery sweep for the outbox (EventBridge Scheduler).
5. CloudWatch: log groups with finite retention, alarms on failed executions, stuck outbox rows and Aurora capacity.
6. Budgets and safeguards: AWS Budgets alerts, a concurrency cap, and a bounded AI spend limit.

Reviewers: security, database.

## Phase 5 — Frontend polling and local end-to-end

No AWS.

- Frontend moves to the polling endpoint with the same UI behaviour. Frontend gates and accessibility review (`ecc:react-reviewer`, `ecc:a11y-architect`).
- Run the whole path locally: Lambda handler, outbox dispatcher, bounded worker, local Postgres.
- Failure drills: kill the worker mid-investigation, send duplicate events, restart the dispatcher, expire a lease.
- Validate authorisation, audit records and policy enforcement through the new path. Tier-3 actions still have no executor.
- `eval/run.py --suite smoke` green.

Aurora pausing, cold starts and Step Functions behaviour cannot be proved locally and are deferred to phase 7.

## Phase 6 — Pre-deploy hardening and documentation

- Deploy workflow for `scale-zero`: `workflow_dispatch` only, a plan job separate from the apply job, apply behind a GitHub Environment with reviewers. Destructive plans stop for approval.
- Read-only `terraform plan` against the target account, and a cost estimate checked against the budget.
- Runbook `docs/scale-to-zero-runbook.md`: deploy, cutover, rollback, how to confirm Aurora paused.
- Full security review of the diff across phases 1–6.
- Definition of done (ESD §44): typed interfaces, defined failure behaviour, enforced permissions, metrics, traces, evaluation coverage, passing tests, working replay and audit, unsafe paths structurally impossible.
- Deprecation notices in `docs/aws-architecture.md` and `docs/terraform.md` are drafted but not applied. Those files are authored separately, so they change only with explicit approval.

## Phase 7 — Deploy to AWS and validate

**Gate:** phases 1–6 merged, pushed and green, and explicit approval to deploy. Not started before that.

1. `plan`, review, approve, `apply`, run migrations as a one-off task, smoke test.
2. Validation on real AWS:
   - API cold start and database wake-up latency.
   - Successful investigation after prolonged inactivity.
   - Retries, worker interruption, checkpoint recovery and duplicate events.
   - Authorisation, audit records and policy enforcement.
   - Tasks terminate after completion.
   - Aurora actually pauses: `ServerlessDatabaseCapacity` reaches 0 after the idle window.
   - Spending alerts fire, log retention is set, AI usage is bounded.
3. Cut Vercel over to the API Gateway URL. Leave the old stack idle for a rollback window.
4. Deprecate the old stack (below) only after the window closes without rollback.

If any check fails, fix it in a new worktree through the same loop. Do not patch AWS by hand.

## Deprecation of the existing AWS infrastructure

The scale-zero environment replaces the ECS-service, ALB and RDS deployment, but only after it has proved itself. The old stack stays available for rollback until then.

| Existing | Fate | Condition |
|---|---|---|
| `infra/terraform/environments/hackathon` and `deploy-hackathon.yml` | Deprecate first | After phase 7 validation passes |
| `infra/terraform/environments/staging` and `deploy-staging.yml` | Undecided | Decide after one full validation cycle |
| `infra/terraform/environments/production` and `deploy-production.yml` | Keep | Not replaced by this plan |
| Modules `compute`, `network`, `queue` (ALB, always-on worker service, NAT and VPC layout, inert SQS queue) | Kept while any environment uses them | Remove only when none does |

Order: cutover, rollback window, mark deprecated in the docs, `terraform destroy` as a separately approved step, remove workflow and modules in a later change. Never destroy before a backup of the old database has been restored successfully once.

## Final recommendation

Use API Gateway + Lambda + Aurora Serverless at 0 ACUs + Step Functions + on-demand ECS Fargate + S3.

Target zero idle compute charges while accepting usage-based costs for storage, database I/O, logs, orchestration and AI inference.

This requires application refactoring in addition to Terraform. Prioritise correctness and end-to-end functionality over achieving zero cost through unsafe shortcuts.


