"use client";

// Live investigation view: incident detail + SSE stream of agent activity (ESD §5).
// The agent reasoning itself is rendered by <AgentTimeline>, which owns the explainability
// contract; this page owns the incident header, state history and the resolve control.

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { AgentTimeline } from "@/components/AgentTimeline";
import { SeverityBadge, StateBadge } from "@/components/badges";
import { LiveIndicator } from "@/components/LiveIndicator";
import { LocalTime } from "@/components/LocalTime";
import {
  api,
  ApiError,
  canAct,
  deniedReason,
  type IncidentDetail,
  type User,
} from "@/lib/api";
import { useEventStream } from "@/lib/useEventStream";

const RESOLVABLE_STATES = ["hypothesis_formed", "monitoring", "remediation_proposed"];

const STREAM_EVENTS = [
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
] as const;

export default function IncidentPage() {
  const params = useParams<{ id: string }>();
  const id = params.id;

  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);

  useEffect(() => {
    api<User>("/auth/me")
      .then(setUser)
      .catch(() => setUser(null));
  }, []);

  const load = useCallback(() => {
    if (!id) return;
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
        setError(err instanceof Error ? err.message : "Failed to load this incident.");
      });
  }, [id]);

  useEffect(load, [load]);

  const { status, retryNow } = useEventStream(id ? `/incidents/${id}/stream` : null, STREAM_EVENTS, load);

  async function resolve() {
    if (!detail) return;
    setResolving(true);
    setActionError(null);
    try {
      await api(`/incidents/${detail.id}/resolve`, { method: "POST" });
      load();
    } catch (err) {
      // A 403 here means the server refused the role — surface it verbatim rather than
      // letting the click look like it did nothing.
      setActionError(
        err instanceof ApiError
          ? `${err.status === 403 ? "Not permitted" : "Failed"}: ${err.message}`
          : "Failed to resolve this incident.",
      );
    } finally {
      setResolving(false);
    }
  }

  if (error) {
    return (
      <div className="rounded-2xl border border-danger/40 bg-danger/5 px-6 py-10 text-center">
        <p className="text-[15px] text-danger">{error}</p>
        <button
          type="button"
          onClick={load}
          className="mt-4 rounded-full border border-edge px-4 py-1.5 text-[12px] transition-colors hover:bg-surface2"
        >
          Try again
        </button>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="space-y-3" aria-busy="true" aria-live="polite">
        <span className="sr-only">Loading incident…</span>
        <div className="h-8 w-2/3 animate-pulse rounded-lg bg-surface2" />
        <div className="h-32 animate-pulse rounded-2xl bg-surface2" />
        <div className="h-24 animate-pulse rounded-2xl bg-surface2" />
      </div>
    );
  }

  const resolvable = RESOLVABLE_STATES.includes(detail.state);
  const denied = deniedReason(user, "act");

  return (
    <div className="space-y-8">
      <div>
        <div className="flex flex-wrap items-center gap-3">
          <SeverityBadge severity={detail.severity} />
          <h1 className="display text-2xl">{detail.title}</h1>
          <StateBadge state={detail.state} />
          <LiveIndicator status={status} onRetry={retryNow} className="ml-auto" />
        </div>

        <p className="mt-2 text-[13px] text-muted">
          {detail.service_name} · {detail.alert_source} · opened{" "}
          <LocalTime iso={detail.created_at} withDate /> ·{" "}
          <Link
            href={`/replay/${detail.id}`}
            className="rounded underline-offset-4 hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
          >
            open replay
          </Link>
        </p>

        {detail.state === "escalated" && (
          <div className="mt-4 rounded-xl border border-danger/40 bg-danger/5 p-4">
            <p className="text-[13px] font-medium text-danger">
              Escalated — automation has stopped.
            </p>
            <p className="mt-1 text-[12.5px] text-muted">
              No further autonomous action will be taken on this incident. It is waiting on a
              human.
            </p>
          </div>
        )}

        {resolvable && (
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              onClick={resolve}
              disabled={!canAct(user) || resolving}
              title={denied ?? "Mark this incident resolved"}
              className="rounded-full bg-inverse-bg px-4 py-1.5 text-[12px] font-medium text-inverse-fg transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-30"
            >
              {resolving ? "Resolving…" : "Mark resolved"}
            </button>
            {denied && <span className="text-[11.5px] text-muted">{denied}</span>}
          </div>
        )}

        {actionError && <p className="mt-3 text-[12.5px] text-danger">{actionError}</p>}
      </div>

      <AgentTimeline steps={detail.steps} />

      {detail.messages.length > 0 && (
        <section>
          <h2 className="mb-3 text-[10px] uppercase tracking-[0.16em] text-muted">
            Stakeholder updates
          </h2>
          <ol className="space-y-2">
            {detail.messages.map((message) => (
              <li key={message.id} className="rounded-xl border border-edge bg-surface p-4">
                <div className="mb-1.5 flex items-center gap-3">
                  <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
                    {message.agent_name}
                  </span>
                  <span className="text-[11px] text-muted">
                    <LocalTime iso={message.created_at} />
                  </span>
                </div>
                <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed">
                  {message.content}
                </p>
              </li>
            ))}
          </ol>
        </section>
      )}

      <section>
        <h2 className="mb-3 text-[10px] uppercase tracking-[0.16em] text-muted">State history</h2>
        {detail.transitions.length === 0 ? (
          <p className="text-[13px] text-muted">No transitions recorded yet.</p>
        ) : (
          <ol className="flex flex-wrap items-center gap-2">
            {detail.transitions.map((transition, i) => (
              <li key={i} className="flex items-center gap-2">
                {i === 0 && <StateBadge state={transition.from_state} />}
                <span aria-hidden="true" className="text-muted">
                  →
                </span>
                <StateBadge state={transition.to_state} />
              </li>
            ))}
          </ol>
        )}
      </section>
    </div>
  );
}
