"use client";

// Incident list, push-updated over the all-incident SSE stream (no polling, ESD §5).

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { SeverityBadge, StateBadge } from "@/components/badges";
import { api, ApiError, eventSource, type Incident } from "@/lib/api";

export default function DashboardPage() {
  const [incidents, setIncidents] = useState<Incident[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);
  const sourceRef = useRef<EventSource | null>(null);

  const load = useCallback(() => {
    api<Incident[]>("/incidents")
      .then((rows) => {
        setIncidents(rows);
        setError(null);
      })
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          window.location.href = "/login";
          return;
        }
        setError(err instanceof Error ? err.message : "failed to load");
      });
  }, []);

  useEffect(() => {
    load();
    const source = eventSource("/incidents/stream");
    sourceRef.current = source;
    source.onopen = () => setLive(true);
    source.onerror = () => setLive(false);
    // Any incident event may change list rows; refetch is cheap and race-free.
    const refresh = () => load();
    for (const type of [
      "incident_created",
      "state_changed",
      "alert_merged",
      "hypothesis",
      "investigation_complete",
    ]) {
      source.addEventListener(type, refresh);
    }
    return () => source.close();
  }, [load]);

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-semibold">Incidents</h1>
        <span
          className={`text-xs ${live ? "text-emerald-400" : "text-slate-500"}`}
          title="SSE connection state"
        >
          ● {live ? "live" : "offline"}
        </span>
      </div>
      {error && <p className="mb-3 text-sm text-red-400">{error}</p>}
      {incidents === null ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : incidents.length === 0 ? (
        <p className="text-sm text-slate-400">
          No incidents yet. Inject a failure into Meridian to create one.
        </p>
      ) : (
        <div className="overflow-hidden rounded-xl border border-edge">
          <table className="w-full text-sm">
            <thead className="bg-panel text-left text-xs uppercase text-slate-400">
              <tr>
                <th className="px-4 py-2">Severity</th>
                <th className="px-4 py-2">Title</th>
                <th className="px-4 py-2">Service</th>
                <th className="px-4 py-2">State</th>
                <th className="px-4 py-2">Opened</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody>
              {incidents.map((incident) => (
                <tr
                  key={incident.id}
                  className="border-t border-edge/60 hover:bg-panel/60"
                >
                  <td className="px-4 py-2">
                    <SeverityBadge severity={incident.severity} />
                  </td>
                  <td className="px-4 py-2">
                    <Link
                      href={`/incidents/${incident.id}`}
                      className="font-medium text-slate-100 hover:text-sky-300"
                    >
                      {incident.title}
                    </Link>
                  </td>
                  <td className="px-4 py-2 text-slate-300">{incident.service_name}</td>
                  <td className="px-4 py-2">
                    <StateBadge state={incident.state} />
                  </td>
                  <td className="px-4 py-2 text-slate-400">
                    {new Date(incident.created_at).toLocaleTimeString()}
                  </td>
                  <td className="px-4 py-2 text-right">
                    <Link
                      href={`/replay/${incident.id}`}
                      className="text-xs text-slate-400 hover:text-sky-300"
                    >
                      replay →
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
