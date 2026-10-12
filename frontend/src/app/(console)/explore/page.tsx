'use client';

import { Suspense } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useQuery } from '@tanstack/react-query';
import { ArrowRight, Beaker, CheckCircle2, Circle } from 'lucide-react';
import { SeverityBadge } from '@/components/ui/primitives';
import { EmptyState, QueryFailure, SkeletonRows } from '@/components/ui/states';
import { api } from '@/lib/api';
import { sampleScenario, type SampleScenario } from '@/lib/sample-scenarios';

const STEPS = [
  ['Alert', 'Read the symptom exactly as the monitoring source reported it.'],
  ['Evidence', 'Inspect metrics, traces, logs and topology without assuming a cause.'],
  ['Hypothesis', 'Compare explanations and keep uncertainty visible.'],
  ['Decision', 'Propose only actions supported by evidence and policy.'],
] as const;

function ExploreContent() {
  const requested = useSearchParams().get('scenario');
  const sample = useQuery({ queryKey: ['sample-incidents'], queryFn: api.getSampleIncidents });
  if (sample.isLoading) return <div className="mx-auto max-w-5xl px-4 py-8"><SkeletonRows rows={4} /></div>;
  if (sample.error || !sample.data) return <div className="mx-auto max-w-5xl px-4 py-8"><QueryFailure error={sample.error ?? new Error('Sample environment unavailable')} title="Sample environment could not be loaded" source="Sample environment" consequence="The isolated walkthrough is unavailable; no live workspace data was substituted." onRetry={() => void sample.refetch()} /></div>;
  const scenarios: SampleScenario[] = sample.data.items.map((item) => ({
    id: item.id, version: item.version, title: item.title, severity: item.severity,
    service: item.service, signal: item.signal, summary: item.summary,
    learningGoal: item.learning_goal,
  }));
  const selected = sampleScenario(scenarios, requested ?? '') ?? scenarios[0];
  if (!selected) return <div className="mx-auto max-w-5xl px-4 py-8"><EmptyState title="No sample scenarios are configured" detail="The sample source was consulted successfully and returned no scenarios." /></div>;

  return (
    <div className="mx-auto max-w-5xl px-4 py-6 sm:px-6 sm:py-8">
      <div className="mb-6 border border-status-info/30 bg-status-info/5 p-4" role="status">
        <div className="flex items-start gap-3">
          <Beaker className="mt-0.5 h-4 w-4 shrink-0 text-status-info" aria-hidden />
          <div>
            <p className="text-body font-semibold text-status-info">Sample environment</p>
            <p className="mt-1 text-meta text-ink-secondary">
              This isolated walkthrough uses a deterministic reference scenario. It never reads,
              changes or appears beside live workspace incidents.
            </p>
          </div>
        </div>
      </div>

      <header className="mb-7 max-w-2xl">
        <p className="label-meta">First-run exploration</p>
        <h1 className="mt-2 text-h2 font-medium tracking-tight">Learn the incident response loop</h1>
        <p className="mt-2 text-body text-ink-secondary">
          Follow the same evidence-first sequence Aegis uses in a live incident. The sample shows
          the workflow, never a fabricated live result.
        </p>
      </header>

      <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
        <nav aria-label="Sample scenarios">
          <h2 className="label-meta mb-2">Choose a scenario</h2>
          <ul className="space-y-2">
            {scenarios.map((scenario) => {
              const active = scenario.id === selected.id;
              return (
                <li key={scenario.id}>
                  <Link
                    href={`/explore?scenario=${encodeURIComponent(scenario.id)}`}
                    aria-current={active ? 'page' : undefined}
                    className={`block rounded-card border p-3 transition-colors duration-hover ${
                      active
                        ? 'border-status-info/40 bg-status-info/5'
                        : 'border-hairline bg-surface-1 hover:bg-surface-2'
                    }`}
                  >
                    <span className="font-mono text-meta text-ink-tertiary">{scenario.id} · v{scenario.version}</span>
                    <span className="mt-1 block text-body text-ink-primary">{scenario.title}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <section aria-labelledby="sample-alert" className="space-y-5">
          <div className="card p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="label-meta">Observed sample alert</p>
                <h2 id="sample-alert" className="mt-2 text-h3 font-semibold">{selected.title}</h2>
              </div>
              <SeverityBadge severity={selected.severity} />
            </div>
            <dl className="mt-5 grid gap-4 border-t border-hairline pt-4 sm:grid-cols-2">
              <div><dt className="label-meta">Service</dt><dd className="mt-1 font-mono text-meta">{selected.service}</dd></div>
              <div><dt className="label-meta">Signal</dt><dd className="mt-1 font-mono text-meta">{selected.signal}</dd></div>
              <div className="sm:col-span-2"><dt className="label-meta">Source summary</dt><dd className="mt-1 text-body text-ink-secondary">{selected.summary}</dd></div>
            </dl>
          </div>

          <div>
            <h2 className="label-meta mb-3">Response path</h2>
            <ol className="space-y-2">
              {STEPS.map(([label, detail], index) => (
                <li key={label} className="flex gap-3 border-b border-hairline py-3 last:border-0">
                  {index === 0 ? <CheckCircle2 className="mt-0.5 h-4 w-4 text-status-info" aria-hidden /> : <Circle className="mt-0.5 h-4 w-4 text-ink-tertiary" aria-hidden />}
                  <div><h3 className="text-body font-medium">{label}</h3><p className="mt-0.5 text-meta text-ink-secondary">{detail}</p></div>
                </li>
              ))}
            </ol>
          </div>

          <div className="flex flex-col gap-3 border-t border-hairline pt-5 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-meta text-ink-secondary">Learning goal: {selected.learningGoal}</p>
            <Link href="/incidents" className="inline-flex items-center gap-1.5 text-body font-medium text-status-info hover:text-ink-primary">
              Open live incidents <ArrowRight className="h-3.5 w-3.5" aria-hidden />
            </Link>
          </div>
        </section>
      </div>
    </div>
  );
}

export default function ExplorePage() {
  return (
    <Suspense fallback={<div className="mx-auto max-w-5xl px-4 py-8"><SkeletonRows rows={4} /></div>}>
      <ExploreContent />
    </Suspense>
  );
}
