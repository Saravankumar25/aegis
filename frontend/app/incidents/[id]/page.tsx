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
  type User,
} from "@/lib/api";

const RESOLVABLE_STATES = ["hypothesis_formed", "monitoring", "remediation_proposed"];

function HypothesisCard({ step }: { step: AgentStep }) {
  const output = step.structured_output as {
    hypothesis?: string;
    root_cause_category?: string;
    agreement_score?: number;
    low_confidence?: boolean;
  } | null;
  if (!output?.hypothesis) return null;
  return (
    <div className="rounded-2xl border border-edge bg-surface p-6">
      <div className="mb-2 flex flex-wrap items-center gap-3">
        <span className="text-[10px] uppercase tracking-[0.16em] text-muted">
          Root-cause hypothesis
        </span>
        {output.low_confidence && (
          <span className="rounded-full border border-warn/40 px-2 py-0.5 text-[11px] text-warn">
            low confidence — ensemble disagreement
          </span>
        )}
      </div>
      <p className="text-[19px] leading-snug tracking-tight">{output.hypothesis}</p>
      <p className="mt-3 text-[12px] text-muted">
        category {output.root_cause_category} · confidence {step.confidence?.toFixed(2) ?? "?"} ·
        agreement {output.agreement_score?.toFixed(2) ?? "?"}
      </p>
      {step.citations.length > 0 && (
        <ul className="mt-5 space-y-2">
          {step.citations.map((citation) => (
            <li key={citation.id} className="rounded-xl border border-edge bg-bg p-3.5">
              <div className="mb-2 flex flex-wrap items-center gap-2 text-[10.5px] text-muted">
                <span className="rounded border border-edge px-1.5 py-0.5 uppercase tracking-wide">
                  {citation.evidence_type}
                </span>
                <code>{citation.evidence_ref}</code>
                {citation.validated_by_observer && (
                  <span className="text-ok">✓ observer-validated</span>
                )}
              </div>
              <pre className="whitespace-pre-wrap break-words font-mono text-[11.5px] leading-relaxed text-fg/80">
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
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    api<User>("/auth/me").then(setUser).catch(() => setUser(null));
  }, []);

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
      "resolution",
      "remediation_proposed",
      "remediation_executed",
      "execution_refused",
      "communication",
    ]) {
      source.addEventListener(type, load);
    }
    return () => source.close();
  }, [params.id]);

  if (error) return <p className="text-[13px] text-danger">{error}</p>;
  if (!detail) return <p className="text-[13px] text-muted">Loading…</p>;

  const hypothesisSteps = detail.steps.filter(
    (s) => s.agent_name === "rca" && s.ensemble_pass_index === null && s.citations.length > 0,
  );
  const latestHypothesis = hypothesisSteps[hypothesisSteps.length - 1];
  const canResolve =
    (user?.role === "on_call_engineer" || user?.role === "admin") &&
    RESOLVABLE_STATES.includes(detail.state);

  return (
    <div className="space-y-8">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <SeverityBadge severity={detail.severity} />
          <h1 className="display text-2xl">{detail.title}</h1>
          <StateBadge state={detail.state} />
          <span
            className={`ml-auto flex items-center gap-1.5 text-[11px] ${live ? "text-ok" : "text-muted"}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full ${live ? "bg-ok" : "bg-muted"}`} />
            {live ? "live" : "offline"}
          </span>
          {canResolve && (
            <button
              onClick={async () => {
                await api(`/incidents/${detail.id}/resolve`, { method: "POST" });
                window.location.reload();
              }}
              className="rounded-full bg-inverse-bg px-4 py-1.5 text-[12px] font-medium text-inverse-fg transition-opacity hover:opacity-80"
            >
              Mark resolved
            </button>
          )}
        </div>
        <p className="mt-2 text-[13px] text-muted">
          {detail.service_name} · {detail.alert_source} · opened{" "}
          {new Date(detail.created_at).toLocaleString()} ·{" "}
          <Link href={`/replay/${detail.id}`} className="underline-offset-4 hover:underline">
            open replay
          </Link>
        </p>
      </div>

      {latestHypothesis && <HypothesisCard step={latestHypothesis} />}

      <section>
        <h2 className="mb-3 text-[10px] uppercase tracking-[0.16em] text-muted">
          Agent activity
        </h2>
        <ol className="space-y-2">
          {detail.messages.map((message) => (
            <li key={message.id} className="rounded-xl border border-edge bg-surface p-4">
              <div className="mb-1.5 flex items-center gap-3">
                <AgentChip name={message.agent_name} />
                <span className="text-[11px] text-muted">
                  {new Date(message.created_at).toLocaleTimeString()}
                </span>
              </div>
              <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed">
                {message.content}
              </p>
            </li>
          ))}
          {detail.messages.length === 0 && (
            <li className="text-[13px] text-muted">
              No agent activity yet — the worker will pick this incident up momentarily.
            </li>
          )}
        </ol>
      </section>

      <section>
        <h2 className="mb-3 text-[10px] uppercase tracking-[0.16em] text-muted">
          State history
        </h2>
        <ol className="flex flex-wrap items-center gap-2">
          {detail.transitions.map((transition, i) => (
            <li key={i} className="flex items-center gap-2">
              {i === 0 && <StateBadge state={transition.from_state} />}
              <span className="text-muted">→</span>
              <StateBadge state={transition.to_state} />
            </li>
          ))}
          {detail.transitions.length === 0 && (
            <li className="text-[13px] text-muted">no transitions yet</li>
          )}
        </ol>
      </section>
    </div>
  );
}
