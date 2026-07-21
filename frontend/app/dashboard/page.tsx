"use client";

// Incident list, push-updated over the all-incident SSE stream (no polling, ESD §5).

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { SeverityBadge, StateBadge } from "@/components/badges";
import { api, ApiError, eventSource, type Incident } from "@/lib/api";

export default function DashboardPage() {
  const [incidents, setIncidents] = useState<Incident[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [live, setLive] = useState(false);

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
      "remediation_proposed",
      "remediation_executed",
    ]) {
      source.addEventListener(type, refresh);
    }
    return () => source.close();
  }, [load]);

  return (
    <div>
      <div className="mb-7 flex items-end justify-between">
        <div>
          <h1 className="display text-3xl">Incidents</h1>
          <p className="mt-1.5 text-[13px] text-muted">
            Live investigation feed across Meridian Commerce.
          </p>
        </div>
        <span
          className={`flex items-center gap-1.5 text-[11px] ${live ? "text-ok" : "text-muted"}`}
          title="SSE connection state"
        >
          <span
            className={`h-1.5 w-1.5 rounded-full ${live ? "bg-ok" : "bg-muted"}`}
          />
          {live ? "live" : "offline"}
        </span>
      </div>

      {error && <p className="mb-4 text-[13px] text-danger">{error}</p>}

      {incidents === null ? (
        <p className="text-[13px] text-muted">Loading…</p>
      ) : incidents.length === 0 ? (
        <div className="rounded-2xl border border-edge bg-surface px-6 py-16 text-center">
          <p className="text-[15px]">No incidents yet</p>
          <p className="mt-1.5 text-[13px] text-muted">
            Inject a failure into Meridian and one will appear here within seconds.
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-2xl border border-edge">
          <table className="w-full text-[13px]">
            <thead className="bg-surface text-left text-[10px] uppercase tracking-[0.14em] text-muted">
              <tr>
                <th className="px-5 py-3 font-medium">Severity</th>
                <th className="px-5 py-3 font-medium">Incident</th>
                <th className="px-5 py-3 font-medium">Service</th>
                <th className="px-5 py-3 font-medium">State</th>
                <th className="px-5 py-3 font-medium">Opened</th>
                <th className="px-5 py-3" />
              </tr>
            </thead>
            <tbody>
              {incidents.map((incident) => (
                <tr
                  key={incident.id}
                  className="border-t border-edge transition-colors hover:bg-surface"
                >
                  <td className="px-5 py-3.5">
                    <SeverityBadge severity={incident.severity} />
                  </td>
                  <td className="px-5 py-3.5">
                    <Link
                      href={`/incidents/${incident.id}`}
                      className="font-medium transition-opacity hover:opacity-60"
                    >
                      {incident.title}
                    </Link>
                  </td>
                  <td className="px-5 py-3.5 text-muted">{incident.service_name}</td>
                  <td className="px-5 py-3.5">
                    <StateBadge state={incident.state} />
                  </td>
                  <td className="px-5 py-3.5 text-muted">
                    {new Date(incident.created_at).toLocaleTimeString()}
                  </td>
                  <td className="px-5 py-3.5 text-right">
                    <Link
                      href={`/replay/${incident.id}`}
                      className="text-[12px] text-muted transition-colors hover:text-fg"
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
