"use client";

// Replay mode: step through the persisted agent-decision history (FR-9). Pure DB replay —
// nothing here touches live infrastructure (FR-9.2).

import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { AgentChip, SeverityBadge, StateBadge } from "@/components/badges";
import { api, ApiError, type Replay } from "@/lib/api";

export default function ReplayPage() {
  const params = useParams<{ id: string }>();
  const [replay, setReplay] = useState<Replay | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [position, setPosition] = useState(0);

  useEffect(() => {
    api<Replay>(`/incidents/${params.id}/replay`)
      .then(setReplay)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          window.location.href = "/login";
          return;
        }
        setError(err instanceof Error ? err.message : "failed to load");
      });
  }, [params.id]);

  const visible = useMemo(
    () => (replay ? replay.events.slice(0, position + 1) : []),
    [replay, position],
  );

  if (error) return <p className="text-[13px] text-danger">{error}</p>;
  if (!replay) return <p className="text-[13px] text-muted">Loading replay…</p>;

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
                <span>{new Date(event.at).toLocaleTimeString()}</span>
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
