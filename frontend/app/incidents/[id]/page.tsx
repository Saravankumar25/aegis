"use client";

// Live investigation view: incident detail + SSE stream of agent activity (ESD §5).

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import { AgentChip, SeverityBadge, StateBadge } from "@/components/badges";
import {
  api,
  ApiError,
  eventSource,
  type AgentStep,
  type IncidentDetail,
} from "@/lib/api";

function HypothesisCard({ step }: { step: AgentStep }) {
  const output = step.structured_output as {
    hypothesis?: string;
    root_cause_category?: string;
    agreement_score?: number;
    low_confidence?: boolean;
  } | null;
  if (!output?.hypothesis) return null;
  return (
    <div className="rounded-xl border border-emerald-500/40 bg-emerald-500/5 p-4">
      <div className="mb-1 flex items-center gap-2 text-xs uppercase text-emerald-300">
        Root-cause hypothesis
        {output.low_confidence && (
          <span className="rounded-full border border-amber-500/50 bg-amber-500/10 px-2 py-0.5 text-amber-300 normal-case">
            low confidence — ensemble disagreement
          </span>
        )}
      </div>
      <p className="text-sm text-slate-100">{output.hypothesis}</p>
      <p className="mt-2 text-xs text-slate-400">
        category {output.root_cause_category} · confidence{" "}
        {step.confidence?.toFixed(2) ?? "?"} · agreement{" "}
        {output.agreement_score?.toFixed(2) ?? "?"}
      </p>
      {step.citations.length > 0 && (
        <ul className="mt-3 space-y-2">
          {step.citations.map((citation) => (
            <li
              key={citation.id}
              className="rounded-md border border-edge bg-surface p-2 text-xs"
            >
              <div className="mb-1 flex items-center gap-2 text-slate-400">
                <span className="uppercase">{citation.evidence_type}</span>
                <code className="text-slate-500">{citation.evidence_ref}</code>
                {citation.validated_by_observer && (
                  <span className="text-emerald-400">✓ observer-validated</span>
                )}
              </div>
              <pre className="whitespace-pre-wrap break-words text-slate-300">
                {citation.evidence_snippet_redacted}
              </pre>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export default function IncidentPage() {
  const params = useParams<{ id: string }>();
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);

  useEffect(() => {
    const id = params.id;
    const load = () =>
      api<IncidentDetail>(`/incidents/${id}`)
        .then((d) => {
          setDetail(d);
          setError(null);
        })
        .catch((err) => {
          if (err instanceof ApiError && err.status === 401) {
            window.location.href = "/login";
            return;
          }
          setError(err instanceof Error ? err.message : "failed to load");
        });
    load();
    const source = eventSource(`/incidents/${id}/stream`);
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);
    for (const type of [
      "agent_step",
      "hypothesis",
      "observer_verdict",
      "state_changed",
      "alert_merged",
      "investigation_complete",
    ]) {
      source.addEventListener(type, load);
    }
    return () => source.close();
  }, [params.id]);

  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (!detail) return <p className="text-sm text-slate-400">Loading…</p>;

  const hypothesisSteps = detail.steps.filter(
    (s) => s.agent_name === "rca" && s.ensemble_pass_index === null && s.citations.length > 0,
  );
  const latestHypothesis = hypothesisSteps[hypothesisSteps.length - 1];

  return (
    <div className="space-y-6">
      <div>
        <div className="flex items-center gap-3">
          <SeverityBadge severity={detail.severity} />
          <h1 className="text-xl font-semibold">{detail.title}</h1>
          <StateBadge state={detail.state} />
          <span
            className={`ml-auto text-xs ${live ? "text-emerald-400" : "text-slate-500"}`}
          >
            ● {live ? "live" : "offline"}
          </span>
        </div>
        <p className="mt-1 text-sm text-slate-400">
          {detail.service_name} · {detail.alert_source} · opened{" "}
          {new Date(detail.created_at).toLocaleString()} ·{" "}
          <Link href={`/replay/${detail.id}`} className="text-sky-400 hover:underline">
            open replay
          </Link>
        </p>
      </div>

      {latestHypothesis && <HypothesisCard step={latestHypothesis} />}

      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase text-slate-400">
          Agent activity
        </h2>
        <ol className="space-y-2">
          {detail.messages.map((message) => (
            <li
              key={message.id}
              className="rounded-lg border border-edge bg-panel p-3 text-sm"
            >
              <div className="mb-1 flex items-center gap-2">
                <AgentChip name={message.agent_name} />
                <span className="text-xs text-slate-500">
                  {new Date(message.created_at).toLocaleTimeString()}
                </span>
              </div>
              <p className="whitespace-pre-wrap text-slate-200">{message.content}</p>
            </li>
          ))}
          {detail.messages.length === 0 && (
            <li className="text-sm text-slate-400">
              No agent activity yet — the worker will pick this incident up momentarily.
            </li>
          )}
        </ol>
      </section>

      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase text-slate-400">
          State history
        </h2>
        <ol className="flex flex-wrap items-center gap-2 text-xs text-slate-300">
          {detail.transitions.map((transition, i) => (
            <li key={i} className="flex items-center gap-2">
              {i === 0 && <StateBadge state={transition.from_state} />}
              <span className="text-slate-500">→</span>
              <StateBadge state={transition.to_state} />
            </li>
          ))}
          {detail.transitions.length === 0 && <li>no transitions yet</li>}
        </ol>
      </section>
    </div>
  );
}
