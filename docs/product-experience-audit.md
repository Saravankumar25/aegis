# Aegis product-experience audit — Phase 1 baseline

**Scope:** implementation-backed discovery for the six-phase plan, updated with
the Phase 1–3 acceptance results. This is not a claim that later-phase UX work
is already complete.

## Current workflow map

| User goal | Current surface | Supporting implementation | Baseline state |
|---|---|---|---|
| Understand the product | `/` marketing route | `LandingContent`, `FlowTimeline`, `SignInPanel` | Implemented; static narrative, not cinematic |
| Authenticate | landing auth panel, console guard | Firebase Google/email, sign-up/reset, emulator development and memory-only bearer tokens in `frontend/src/lib/auth.ts` | Complete for Phase 2; no public token bypass |
| See operational posture | `/overview` | console API summary and grouped navigation | Implemented |
| Find incidents | `/incidents` | filterable incident API/list | Implemented; errors distinct from empty state |
| Investigate an incident | `/incidents/[id]`, `/investigations/[incidentId]` | incident, evidence, timeline, hypotheses, diagnosis and event stream APIs | Implemented but context links are incomplete |
| Explore services | `/systems`, `/systems/[serviceId]` | service inventory, metrics and instances | Implemented; service detail links to graph |
| Explore topology | `/graph` | graph inventory, neighbourhood, blast-radius and path APIs | Substantial bounded SVG implementation; contextual data is partial |
| Review approvals | `/approvals` | approval APIs and dialogs | Implemented |
| Inspect changes/results | `/deployments`, `/audit`, `/reliability`, `/evaluation` | typed APIs and dedicated components | Implemented as separate destinations; continuity needs review |
| Operate a live demo | `/war-room` | streamed Horizon workflow and controlled fault actions | Implemented operator demo, not first-run onboarding |
| Configure controls | `/policies`, `/settings` | policy/integration/autonomy surfaces | Implemented |

The navigation already groups sixteen destinations by operational goal in
`frontend/src/components/shell/navigation.ts`; it should be refined, not reduced
to an arbitrary minimal sidebar.

## Evidence-backed strengths to preserve

- The public timeline is labelled illustrative rather than presented as live.
- Incident evidence exposes source-unavailable separately from an empty result.
- New Firebase identities receive no Aegis role; tenant, environment and role
  claims must be provisioned before operational data is available.
- Service detail deep-links into graph context.
- The current graph has deterministic bounded layout, keyboard node traversal,
  fit/zoom/pan controls and a readable list alternative.
- Global reduced-motion CSS exists.
- The API remains the authorization boundary; the console route guard is only a
  user-experience guard.
- War-room actions run real backend operations rather than disconnected UI
  demonstrations.

## Gap register

| Gap | State | Risk | Planned phase |
|---|---|---|---|
| No isolated in-product sample environment or onboarding journey | Completed in `/explore` | Deterministic samples remain isolated from live incidents | 2 contract, 3 UI |
| Public development-token form | Removed | Firebase Auth Emulator preserves local development without a published bypass | 2 |
| Sign-up and password reset | Completed | Firebase email and Google entry share the same server-verifiable identity contract | 2 |
| Mobile console navigation | Completed for Phase 3 | Radix dialog provides focus trap, Escape, overlay and focus restoration | 3 |
| Incident → service → graph → evidence continuity | Partial | Users lose context and repeat navigation | 4–5 |
| Graph related incidents/evidence/telemetry | Missing API/UI association | Node selection cannot answer the next operational question | 4 contracts, 5 UI |
| First-class observed/derived/inferred/sample labels | Partial and inconsistent | Users may over-trust a result | 3–6 |
| Route-level and browser E2E tests | Missing | Workflow regressions can pass component tests | 3 incremental, 6 gate |
| Automated accessibility checks | Missing | Keyboard/semantic regressions are not release-gated | 3 incremental, 6 gate |
| Graph scale benchmarks beyond the 120-node cap | Missing | Library replacement would be aesthetic guesswork | 5 |
| Cinematic landing scene | Missing | New creative brief is unmet | 5 |

## Interaction and information contract

Every operation must keep these five elements connected:

1. **Context:** incident, service, environment and observed state.
2. **Authority:** caller role, policy result, approval and action tier.
3. **Action:** what will happen, blast radius and rollback/verification plan.
4. **Progress:** queued, running, awaiting approval, failed, unavailable or done.
5. **Result:** persisted outcome, evidence, verification and the next logical
   action.

Buttons cannot imply success before the backend result exists. Permission
denials, unavailable integrations, empty results and insufficient evidence need
separate language and separate states.

## Authoritative-spec decisions

| Topic | Decision for the plan |
|---|---|
| Audience expansion | General engineers are a secondary exploration-mode persona until the PRD is revised; operational personas remain primary. |
| Polling versus SSE | Treat polling as a proposed transport revision. Preserve ordered event ids/replay and keep SSE locally until the ESD is revised. |
| Marketing priority | Operational incident workflows precede cinematic work, matching the UI spec’s implementation priority. |
| Motion restrictions | Cinematic motion is confined to the public landing page, with reduced-motion, non-WebGL and hidden-tab behavior. Operational screens retain restrained motion. |
| Graph technology | Benchmark current SVG behavior and representative topologies before selecting one specialized library. |
| Development auth | Remove public token entry only when emulator/test auth preserves local development without weakening production. |
| Sample data | Reuse deterministic reference scenarios; isolate them server-side and label them persistently. |

## Phase 1–3 acceptance record

- Phase 1: the baseline, product-state contract, durable dispatch, lease
  fencing, bounded worker and supervisor-owned budgets are implemented and
  verified.
- Phase 2: Firebase entry/session recovery, environment-scoped APIs,
  server-side sample contracts, replay-safe polling, Lambda adapter and
  database-resume behavior are implemented and verified.
- Phase 3: responsive navigation, accessible mobile focus management,
  breadcrumbs, related destinations, shared permission language, isolated
  server-backed exploration and the plan-only scale-zero foundation are
  implemented and verified.

The remaining gap register entries continue to govern Phases 4–6; completing
these three phases does not claim that operational continuity, the graph or the
cinematic landing work is finished.
