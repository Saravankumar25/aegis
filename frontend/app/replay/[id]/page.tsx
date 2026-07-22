"use client";

// Replay mode: step through the persisted agent-decision history (FR-9). Pure DB replay —
// nothing here touches live infrastructure (FR-9.2).

import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { LocalTime } from "@/components/LocalTime";
import { AgentChip, SeverityBadge, StateBadge } from "@/components/badges";
import { api, ApiError, type Replay } from "@/lib/api";

export default function ReplayPage() {
  const params = useParams<{ id: string }>();
  const [replay, setReplay] = useState<Replay | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [position, setPosition] = useState(0);

  const load = useCallback(() => {
    setError(null);
    api<Replay>(`/incidents/${params.id}/replay`)
      .then(setReplay)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          window.location.href = "/login";
          return;
        }
        setError(err instanceof Error ? err.message : "Failed to load this replay.");
      });
  }, [params.id]);

  useEffect(load, [load]);

  const visible = useMemo(
    () => (replay ? replay.events.slice(0, position + 1) : []),
    [replay, position],
  );

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

  if (!replay) {
    return (
      <div className="space-y-3" aria-busy="true" aria-live="polite">
        <span className="sr-only">Loading replay…</span>
        <div className="h-8 w-1/2 animate-pulse rounded-lg bg-surface2" />
        <div className="h-14 animate-pulse rounded-2xl bg-surface2" />
        <div className="h-24 animate-pulse rounded-2xl bg-surface2" />
      </div>
    );
  }

  if (replay.events.length === 0) {
    return (
      <div className="space-y-5">
        <div className="flex flex-wrap items-center gap-3">
          <SeverityBadge severity={replay.incident.severity} />
          <h1 className="display text-2xl">Replay — {replay.incident.title}</h1>
          <StateBadge state={replay.incident.state} />
        </div>
        <div className="rounded-2xl border border-edge bg-surface px-6 py-16 text-center">
          <p className="text-[15px]">Nothing to replay yet</p>
          <p className="mt-1.5 text-[13px] text-muted">
            Replay is reconstructed from persisted rows, so events appear here once the
            investigation has recorded its first step.
          </p>
        </div>
      </div>
    );
  }

  const last = replay.events.length - 1;
  const stepButton =
    "rounded-full border border-edge px-4 py-1.5 text-[12px] transition-colors hover:bg-surface2 disabled:opacity-30";

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <SeverityBadge severity={replay.incident.severity} />
        <h1 className="display text-2xl">Replay — {replay.incident.title}</h1>
        <StateBadge state={replay.incident.state} />
      </div>

      <div className="flex items-center gap-4 rounded-2xl border border-edge bg-surface p-4">
        <button
          onClick={() => setPosition((p) => Math.max(0, p - 1))}
          disabled={position === 0}
          className={stepButton}
        >
          ← prev
        </button>
        <input
          type="range"
          min={0}
          max={Math.max(last, 0)}
          value={position}
          onChange={(e) => setPosition(Number(e.target.value))}
          className="h-1 flex-1 cursor-pointer appearance-none rounded-full bg-edge accent-fg"
        />
        <button
          onClick={() => setPosition((p) => Math.min(last, p + 1))}
          disabled={position >= last}
          className={stepButton}
        >
          next →
        </button>
        <span className="w-20 text-right font-mono text-[11px] text-muted">
          {position + 1} / {replay.events.length}
        </span>
      </div>

      <ol className="space-y-2">
        {visible.map((event) => {
          const current = event.sequence === position;
          return (
            <li
              key={event.sequence}
              className={`rounded-xl border p-4 transition-colors ${
                current ? "border-fg/30 bg-surface2" : "border-edge bg-surface"
              }`}
            >
              <div className="mb-1.5 flex flex-wrap items-center gap-3 text-[11px] text-muted">
                <span className="font-mono">#{event.sequence + 1}</span>
                <span className="uppercase tracking-[0.14em]">{event.kind}</span>
                {event.agent_name && <AgentChip name={event.agent_name} />}
                <span><LocalTime iso={event.at} /></span>
              </div>
              <p className="text-[13.5px] leading-relaxed">{event.summary}</p>
              {current && (
                <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-lg border border-edge bg-bg p-3 font-mono text-[11px] leading-relaxed text-muted">
                  {JSON.stringify(event.detail, null, 2)}
                </pre>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
