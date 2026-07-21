"use client";

// Public marketing homepage. Apple-style: enormous type, centred composition,
// one idea per screen, product imagery doing the talking, monochrome throughout.

import Link from "next/link";
import { ThemeToggle } from "@/components/ThemeToggle";

function MarketingNav() {
  return (
    <header className="sticky top-0 z-50 border-b border-edge/70 bg-bg/70 backdrop-blur-xl">
      <div className="mx-auto flex h-12 max-w-5xl items-center gap-8 px-6">
        <Link href="/" className="text-[15px] font-semibold tracking-tight">
          Aegis
        </Link>
        <nav className="hidden gap-7 text-[12px] text-muted sm:flex">
          <a href="#how" className="transition-colors hover:text-fg">
            How it works
          </a>
          <a href="#grounded" className="transition-colors hover:text-fg">
            Evidence
          </a>
          <a href="#safety" className="transition-colors hover:text-fg">
            Safety
          </a>
          <a href="#agents" className="transition-colors hover:text-fg">
            Agents
          </a>
        </nav>
        <div className="ml-auto flex items-center gap-4">
          <ThemeToggle />
          <Link
            href="/dashboard"
            className="rounded-full bg-inverse-bg px-4 py-1.5 text-[12px] font-medium text-inverse-fg transition-opacity hover:opacity-80"
          >
            Open dashboard
          </Link>
        </div>
      </div>
    </header>
  );
}

// A schematic of the pipeline — deliberately NOT a mocked-up incident. Showing
// invented severities, confidences and commit hashes on a marketing page would be
// fabricated data dressed as product output, which is the exact thing this system
// is built to refuse. This describes what each stage does; the real numbers live
// on the dashboard, against real incidents.
function PipelineSchematic() {
  const stages = [
    { agent: "Triage", does: "Classifies severity, merges duplicate alerts" },
    { agent: "Correlation", does: "Pulls logs, metrics, events, recent deploys" },
    { agent: "RCA", does: "Reasons in parallel, scores how much the passes agree" },
    { agent: "Observer", does: "Rejects any claim its cited evidence doesn't support" },
    { agent: "Resolution", does: "Acts only inside four independent safety gates" },
  ];
  return (
    <div className="mx-auto w-full max-w-3xl overflow-hidden rounded-2xl border border-edge bg-surface">
      <div className="border-b border-edge px-5 py-3 text-[10px] uppercase tracking-[0.16em] text-muted">
        Alert in → cited hypothesis out
      </div>
      <ol className="divide-y divide-edge">
        {stages.map((stage, i) => (
          <li key={stage.agent} className="flex items-baseline gap-4 px-5 py-4 text-left">
            <span className="w-5 shrink-0 font-mono text-[11px] text-muted">
              {String(i + 1).padStart(2, "0")}
            </span>
            <span className="w-28 shrink-0 text-[13px] font-medium">{stage.agent}</span>
            <span className="text-[13px] leading-snug text-muted">{stage.does}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

function Stat({ value, label }: { value: string; label: string }) {
  return (
    <div className="text-center">
      <p className="display text-4xl sm:text-5xl">{value}</p>
      <p className="mt-2 text-[12px] leading-snug text-muted">{label}</p>
    </div>
  );
}

export default function HomePage() {
  return (
    <div className="bg-bg">
      <MarketingNav />

      {/* Hero */}
      <section className="relative overflow-hidden px-6 pb-20 pt-24 text-center sm:pt-32">
        <div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-[420px] opacity-[0.10]"
          style={{
            background:
              "radial-gradient(60% 100% at 50% 0%, rgb(var(--fg)) 0%, transparent 70%)",
          }}
        />
        <div className="relative reveal">
          <p className="mb-5 text-[12px] uppercase tracking-[0.2em] text-muted">
            Multi-agent incident response
          </p>
          <h1 className="display mx-auto max-w-4xl text-hero">
            Your 2 A.M. page,
            <br />
            already investigated.
          </h1>
          <p className="mx-auto mt-7 max-w-xl text-[17px] leading-relaxed text-muted sm:text-[19px]">
            Aegis reads the logs, the metrics and the deploys the moment an alert fires — and hands
            you a root-cause hypothesis with the evidence attached, before you open your laptop.
          </p>
          <div className="mt-9 flex flex-wrap items-center justify-center gap-3">
            <Link
              href="/dashboard"
              className="rounded-full bg-inverse-bg px-7 py-3 text-[14px] font-medium text-inverse-fg transition-opacity hover:opacity-80"
            >
              Open the dashboard
            </Link>
            <a
              href="#how"
              className="rounded-full border border-edge px-7 py-3 text-[14px] font-medium transition-colors hover:bg-surface"
            >
              See how it works →
            </a>
          </div>
        </div>

        <div className="relative mt-16 reveal sm:mt-20">
          <PipelineSchematic />
        </div>
      </section>

      {/* Numbers */}
      <section className="border-y border-edge bg-surface px-6 py-16">
        <div className="mx-auto grid max-w-4xl grid-cols-2 gap-10 sm:grid-cols-4">
          <Stat value="&lt;3 min" label="P95 to first hypothesis" />
          <Stat value="100%" label="Claims carry a citation" />
          <Stat value="4" label="Gates before any action" />
          <Stat value="0" label="Credentials held by agents" />
        </div>
      </section>

      {/* How it works */}
      <section id="how" className="px-6 py-28 sm:py-36">
        <div className="mx-auto max-w-4xl text-center">
          <h2 className="display text-section">Seven agents. One incident.</h2>
          <p className="mx-auto mt-6 max-w-2xl text-[17px] leading-relaxed text-muted">
            An orchestrator routes every step. No agent calls another directly, so the path an
            investigation took is always reconstructable.
          </p>
        </div>

        <div className="mx-auto mt-16 grid max-w-4xl gap-px overflow-hidden rounded-2xl border border-edge bg-edge sm:grid-cols-2">
          {[
            {
              step: "01",
              title: "An alert arrives",
              body:
                "Ingestion is idempotent and deduplicating — a retried webhook or a second alert for the same failure joins one incident instead of splitting your attention.",
            },
            {
              step: "02",
              title: "Evidence is gathered",
              body:
                "Pod logs, k8s events, Prometheus series and the last two hours of deploys — correlated across time and service topology. Unreachable sources become documented gaps, never silent holes.",
            },
            {
              step: "03",
              title: "A hypothesis is formed",
              body:
                "Three independent reasoning passes produce an agreement score. When they disagree, you see the disagreement — it is never averaged into false confidence.",
            },
            {
              step: "04",
              title: "The Observer checks the work",
              body:
                "Every claim must resolve to a specific piece of gathered evidence. Uncited claims are rejected and the analysis is sent back.",
            },
          ].map((item) => (
            <div key={item.step} className="bg-bg p-8">
              <p className="font-mono text-[11px] text-muted">{item.step}</p>
              <h3 className="mt-3 text-[19px] font-semibold tracking-tight">{item.title}</h3>
              <p className="mt-3 text-[14px] leading-relaxed text-muted">{item.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Grounded */}
      <section id="grounded" className="border-t border-edge bg-surface px-6 py-28 sm:py-36">
        <div className="mx-auto max-w-3xl text-center">
          <h2 className="display text-section">
            Confident is easy.
            <br />
            Grounded is the hard part.
          </h2>
          <p className="mx-auto mt-6 max-w-2xl text-[17px] leading-relaxed text-muted">
            A hypothesis without a citation does not ship. Every claim links to the exact log line,
            metric sample or commit behind it — redacted, and shown to you in full.
          </p>
        </div>

        <div className="mx-auto mt-14 max-w-2xl overflow-hidden rounded-2xl border border-edge bg-bg">
          <div className="border-b border-edge px-5 py-3 text-[10px] uppercase tracking-widest text-muted">
            Illustration of the citation format
          </div>
          {[
            ["metric", "prom/error_rate/checkout-service", 'rate(status="500") = 3.32/s'],
            ["log", "k8s/pod/checkout-5fb95/log", "ERROR handler crashed on cache_ttl=300"],
            ["diff", "github/commit/9f1c2e3", "feat: raise checkout cache TTL to 300s"],
          ].map(([kind, ref, snippet]) => (
            <div key={ref} className="border-b border-edge px-5 py-4 last:border-0">
              <div className="flex flex-wrap items-center gap-2 text-[10.5px] text-muted">
                <span className="rounded border border-edge px-1.5 py-0.5 uppercase tracking-wide">
                  {kind}
                </span>
                <code>{ref}</code>
                <span className="text-ok">✓ validated</span>
              </div>
              <p className="mt-2 font-mono text-[12px] leading-relaxed">{snippet}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Safety */}
      <section id="safety" className="px-6 py-28 sm:py-36">
        <div className="mx-auto max-w-3xl text-center">
          <h2 className="display text-section">
            It can act.
            <br />
            Only inside the lines.
          </h2>
          <p className="mx-auto mt-6 max-w-2xl text-[17px] leading-relaxed text-muted">
            Autonomy is earned one gate at a time. Every remediation passes four independent checks,
            in order, on every single execution — and starts life in shadow mode, where it records
            what it would have done and touches nothing.
          </p>
        </div>

        <div className="mx-auto mt-14 max-w-3xl space-y-px overflow-hidden rounded-2xl border border-edge bg-edge">
          {[
            [
              "Kill switch",
              "One durable flag halts every autonomous action everywhere, immediately — including work already approved.",
            ],
            [
              "Resource lease",
              "Postgres itself allows exactly one in-flight action per target. Two workers racing cannot both win.",
            ],
            [
              "Circuit breakers",
              "Three automatic fixes per service per hour, then autonomy narrows to proposals. A system-wide breaker stops one root cause from becoming a wave of actions.",
            ],
            [
              "Tier gates",
              "Tier 1 auto-executes only on a validated hypothesis within a bounded blast radius. Tier 2 waits for a human. Tier 3 has no machine path at all.",
            ],
          ].map(([title, body], i) => (
            <div key={title} className="flex gap-5 bg-bg p-7 sm:gap-8 sm:p-8">
              <span className="font-mono text-[11px] text-muted">0{i + 1}</span>
              <div>
                <h3 className="text-[17px] font-semibold tracking-tight">{title}</h3>
                <p className="mt-2 text-[14px] leading-relaxed text-muted">{body}</p>
              </div>
            </div>
          ))}
        </div>

        <p className="mx-auto mt-10 max-w-2xl text-center text-[13px] leading-relaxed text-muted">
          Every remediation is defined together with the action that undoes it. There is no forward
          action in Aegis without a documented reverse.
        </p>
      </section>

      {/* Agents */}
      <section id="agents" className="border-t border-edge bg-surface px-6 py-28 sm:py-36">
        <div className="mx-auto max-w-3xl text-center">
          <h2 className="display text-section">The cast</h2>
        </div>
        <div className="mx-auto mt-14 grid max-w-4xl gap-8 sm:grid-cols-3">
          {[
            ["Triage", "Severity and deduplication the moment an alert lands."],
            ["Correlation", "Logs, metrics, events and deploys — across time and topology."],
            ["RCA", "Ensemble reasoning with an honest agreement score."],
            ["Observer", "Validates citations. Screens evidence for injected instructions."],
            ["Resolution", "Tiered remediation behind four safety gates."],
            ["Communication", "Plain-English updates. No jargon, ever."],
          ].map(([name, body]) => (
            <div key={name}>
              <h3 className="text-[15px] font-semibold tracking-tight">{name}</h3>
              <p className="mt-2 text-[13.5px] leading-relaxed text-muted">{body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Close */}
      <section className="px-6 py-32 text-center sm:py-40">
        <h2 className="display mx-auto max-w-3xl text-section">
          Stop starting from
          <br />a blank dashboard.
        </h2>
        <div className="mt-10 flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/dashboard"
            className="rounded-full bg-inverse-bg px-7 py-3 text-[14px] font-medium text-inverse-fg transition-opacity hover:opacity-80"
          >
            Open the dashboard
          </Link>
          <Link
            href="/login"
            className="rounded-full border border-edge px-7 py-3 text-[14px] font-medium transition-colors hover:bg-surface"
          >
            Sign in
          </Link>
        </div>
      </section>

      <footer className="border-t border-edge px-6 py-10">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-4 text-[12px] text-muted">
          <span className="font-semibold tracking-tight text-fg">Aegis</span>
          <span>Multi-agent incident response for Meridian Commerce.</span>
          <div className="ml-auto flex items-center gap-4">
            <Link href="/dashboard" className="transition-colors hover:text-fg">
              Dashboard
            </Link>
            <Link href="/approvals" className="transition-colors hover:text-fg">
              Approvals
            </Link>
            <ThemeToggle />
          </div>
        </div>
      </footer>
    </div>
  );
}
