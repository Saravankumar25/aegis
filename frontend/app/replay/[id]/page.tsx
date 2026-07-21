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

  if (error) return <p className="text-sm text-red-400">{error}</p>;
  if (!replay) return <p className="text-sm text-slate-400">Loading replay…</p>;

  const last = replay.events.length - 1;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <SeverityBadge severity={replay.incident.severity} />
        <h1 className="text-xl font-semibold">Replay — {replay.incident.title}</h1>
        <StateBadge state={replay.incident.state} />
      </div>

      <div className="flex items-center gap-3 rounded-xl border border-edge bg-panel p-3">
        <button
          onClick={() => setPosition((p) => Math.max(0, p - 1))}
          disabled={position === 0}
          className="rounded-md border border-edge px-3 py-1 text-sm hover:bg-surface disabled:opacity-40"
        >
          ← prev
        </button>
        <input
          type="range"
          min={0}
          max={Math.max(last, 0)}
          value={position}
          onChange={(e) => setPosition(Number(e.target.value))}
          className="flex-1 accent-sky-500"
        />
        <button
          onClick={() => setPosition((p) => Math.min(last, p + 1))}
          disabled={position >= last}
          className="rounded-md border border-edge px-3 py-1 text-sm hover:bg-surface disabled:opacity-40"
        >
          next →
        </button>
        <span className="w-20 text-right text-xs text-slate-400">
          {position + 1} / {replay.events.length}
        </span>
      </div>

      <ol className="space-y-2">
        {visible.map((event) => (
          <li
            key={event.sequence}
            className={`rounded-lg border p-3 text-sm ${
              event.sequence === position
                ? "border-sky-500/60 bg-sky-500/5"
                : "border-edge bg-panel"
            }`}
          >
            <div className="mb-1 flex items-center gap-2 text-xs text-slate-500">
              <span className="font-mono">#{event.sequence + 1}</span>
              <span className="uppercase">{event.kind}</span>
              {event.agent_name && <AgentChip name={event.agent_name} />}
              <span>{new Date(event.at).toLocaleTimeString()}</span>
            </div>
            <p className="text-slate-200">{event.summary}</p>
            {event.sequence === position && (
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-edge bg-surface p-2 text-xs text-slate-400">
                {JSON.stringify(event.detail, null, 2)}
              </pre>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
