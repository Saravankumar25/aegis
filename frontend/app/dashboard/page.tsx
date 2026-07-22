"use client";

// Incident list, push-updated over the all-incident SSE stream (no polling, ESD §5).
// Filtering and sorting are client-side: the list is bounded by the API's own page size, and
// a round trip per keystroke would be worse on every axis at this scale.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { SeverityBadge, StateBadge } from "@/components/badges";
import { LiveIndicator } from "@/components/LiveIndicator";
import { LocalTime } from "@/components/LocalTime";
import { api, ApiError, type Incident } from "@/lib/api";
import { useEventStream } from "@/lib/useEventStream";

const STREAM_EVENTS = [
  "incident_created",
  "state_changed",
  "alert_merged",
  "hypothesis",
  "investigation_complete",
  "remediation_proposed",
  "remediation_executed",
  "escalated",
] as const;

type SortKey = "newest" | "oldest" | "severity" | "service";

const SEVERITY_RANK: Record<string, number> = { P1: 0, P2: 1, P3: 2, P4: 3 };

const ANY = "__any__";

const selectClass =
  "rounded-full border border-edge bg-surface px-3 py-1.5 text-[12px] text-fg transition-colors hover:bg-surface2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg";

export default function DashboardPage() {
  const [incidents, setIncidents] = useState<Incident[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [state, setState] = useState<string>(ANY);
  const [severity, setSeverity] = useState<string>(ANY);
  const [service, setService] = useState<string>(ANY);
  const [sort, setSort] = useState<SortKey>("newest");

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
        setError(err instanceof Error ? err.message : "Failed to load incidents.");
      });
  }, []);

  useEffect(load, [load]);

  const { status, retryNow } = useEventStream("/incidents/stream", STREAM_EVENTS, load);

  const states = useMemo(
    () => Array.from(new Set((incidents ?? []).map((i) => i.state))).sort(),
    [incidents],
  );
  const services = useMemo(
    () => Array.from(new Set((incidents ?? []).map((i) => i.service_name))).sort(),
    [incidents],
  );

  const visible = useMemo(() => {
    let rows = incidents ?? [];
    const q = query.trim().toLowerCase();
    if (q) {
      rows = rows.filter(
        (i) =>
          i.title.toLowerCase().includes(q) ||
          i.service_name.toLowerCase().includes(q) ||
          i.external_alert_id.toLowerCase().includes(q),
      );
    }
    if (state !== ANY) rows = rows.filter((i) => i.state === state);
    if (severity !== ANY) rows = rows.filter((i) => i.severity === severity);
    if (service !== ANY) rows = rows.filter((i) => i.service_name === service);

    const sorted = [...rows];
    sorted.sort((a, b) => {
      switch (sort) {
        case "oldest":
          return a.created_at.localeCompare(b.created_at);
        case "severity":
          return (
            (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9) ||
            b.created_at.localeCompare(a.created_at)
          );
        case "service":
          return (
            a.service_name.localeCompare(b.service_name) ||
            b.created_at.localeCompare(a.created_at)
          );
        default:
          return b.created_at.localeCompare(a.created_at);
      }
    });
    return sorted;
  }, [incidents, query, state, severity, service, sort]);

  const escalatedCount = useMemo(
    () => (incidents ?? []).filter((i) => i.state === "escalated").length,
    [incidents],
  );

  const filtersActive =
    query.trim() !== "" || state !== ANY || severity !== ANY || service !== ANY;

  function clearFilters() {
    setQuery("");
    setState(ANY);
    setSeverity(ANY);
    setService(ANY);
  }

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="display text-3xl">Incidents</h1>
          <p className="mt-1.5 text-[13px] text-muted">
            Live investigation feed across Meridian Commerce.
          </p>
        </div>
        <LiveIndicator status={status} onRetry={retryNow} />
      </div>

      {/* Escalated means the automation has stopped and a human is the only thing that will
          move these incidents — it gets a standing banner, not just a row badge. */}
      {escalatedCount > 0 && (
        <button
          type="button"
          onClick={() => setState("escalated")}
          className="mb-5 flex w-full items-center gap-3 rounded-xl border border-danger/40 bg-danger/5 px-4 py-3 text-left transition-colors hover:bg-danger/10"
        >
          <span aria-hidden="true" className="h-2 w-2 rounded-full bg-danger" />
          <span className="text-[13px] font-medium text-danger">
            {escalatedCount} incident{escalatedCount === 1 ? "" : "s"} escalated — a human is
            required
          </span>
          <span className="ml-auto text-[11.5px] text-muted">show only these →</span>
        </button>
      )}

      <div className="mb-5 flex flex-wrap items-center gap-2">
        <label className="flex-1 min-w-[200px]">
          <span className="sr-only">Search incidents</span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search title, service or alert id…"
            className="w-full rounded-full border border-edge bg-surface px-4 py-1.5 text-[13px] text-fg placeholder:text-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
          />
        </label>

        <label>
          <span className="sr-only">Filter by state</span>
          <select value={state} onChange={(e) => setState(e.target.value)} className={selectClass}>
            <option value={ANY}>All states</option>
            {states.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span className="sr-only">Filter by severity</span>
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
            className={selectClass}
          >
            <option value={ANY}>All severities</option>
            {["P1", "P2", "P3", "P4"].map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span className="sr-only">Filter by service</span>
          <select
            value={service}
            onChange={(e) => setService(e.target.value)}
            className={selectClass}
          >
            <option value={ANY}>All services</option>
            {services.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>

        <label>
          <span className="sr-only">Sort incidents</span>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortKey)}
            className={selectClass}
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
            <option value="severity">Severity</option>
            <option value="service">Service</option>
          </select>
        </label>

        {filtersActive && (
          <button
            type="button"
            onClick={clearFilters}
            className="rounded-full border border-edge px-3 py-1.5 text-[12px] text-muted transition-colors hover:bg-surface2 hover:text-fg"
          >
            Clear
          </button>
        )}
      </div>

      {error && (
        <div className="mb-5 rounded-xl border border-danger/40 bg-danger/5 px-4 py-3">
          <p className="text-[13px] text-danger">{error}</p>
          <button
            type="button"
            onClick={load}
            className="mt-2 rounded-full border border-edge px-3 py-1 text-[12px] transition-colors hover:bg-surface2"
          >
            Try again
          </button>
        </div>
      )}

      {incidents === null ? (
        <div className="space-y-2" aria-busy="true" aria-live="polite">
          <span className="sr-only">Loading incidents…</span>
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-14 animate-pulse rounded-xl bg-surface2" />
          ))}
        </div>
      ) : incidents.length === 0 ? (
        <div className="rounded-2xl border border-edge bg-surface px-6 py-16 text-center">
          <p className="text-[15px]">No incidents yet</p>
          <p className="mt-1.5 text-[13px] text-muted">
            Inject a failure into Meridian and one will appear here within seconds.
          </p>
        </div>
      ) : visible.length === 0 ? (
        <div className="rounded-2xl border border-edge bg-surface px-6 py-16 text-center">
          <p className="text-[15px]">No incidents match these filters</p>
          <p className="mt-1.5 text-[13px] text-muted">
            {incidents.length} incident{incidents.length === 1 ? "" : "s"} are hidden by the
            current search and filters.
          </p>
          <button
            type="button"
            onClick={clearFilters}
            className="mt-4 rounded-full border border-edge px-4 py-1.5 text-[12px] transition-colors hover:bg-surface2"
          >
            Clear filters
          </button>
        </div>
      ) : (
        <>
          <p className="mb-2 text-[11.5px] text-muted" aria-live="polite">
            Showing {visible.length} of {incidents.length}
          </p>

          {/* Table on desktop; the same rows become stacked cards under 640px, because a
              six-column table at 375px is unreadable however it is scrolled. */}
          <div className="hidden overflow-hidden rounded-2xl border border-edge sm:block">
            <table className="w-full text-[13px]">
              <caption className="sr-only">Incidents, newest first</caption>
              <thead className="bg-surface text-left text-[10px] uppercase tracking-[0.14em] text-muted">
                <tr>
                  <th scope="col" className="px-5 py-3 font-medium">
                    Severity
                  </th>
                  <th scope="col" className="px-5 py-3 font-medium">
                    Incident
                  </th>
                  <th scope="col" className="px-5 py-3 font-medium">
                    Service
                  </th>
                  <th scope="col" className="px-5 py-3 font-medium">
                    State
                  </th>
                  <th scope="col" className="px-5 py-3 font-medium">
                    Opened
                  </th>
                  <th scope="col" className="px-5 py-3">
                    <span className="sr-only">Replay</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {visible.map((incident) => (
                  <tr
                    key={incident.id}
                    className={`border-t border-edge transition-colors hover:bg-surface ${
                      incident.state === "escalated" ? "bg-danger/5" : ""
                    }`}
                  >
                    <td className="px-5 py-3.5">
                      <SeverityBadge severity={incident.severity} />
                    </td>
                    <td className="px-5 py-3.5">
                      <Link
                        href={`/incidents/${incident.id}`}
                        className="rounded font-medium transition-opacity hover:opacity-60 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
                      >
                        {incident.title}
                      </Link>
                    </td>
                    <td className="px-5 py-3.5 text-muted">{incident.service_name}</td>
                    <td className="px-5 py-3.5">
                      <StateBadge state={incident.state} />
                    </td>
                    <td className="px-5 py-3.5 text-muted">
                      <LocalTime iso={incident.created_at} />
                    </td>
                    <td className="px-5 py-3.5 text-right">
                      <Link
                        href={`/replay/${incident.id}`}
                        className="rounded text-[12px] text-muted transition-colors hover:text-fg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
                      >
                        replay →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <ul className="space-y-2 sm:hidden">
            {visible.map((incident) => (
              <li
                key={incident.id}
                className={`rounded-xl border border-edge bg-surface p-4 ${
                  incident.state === "escalated" ? "border-danger/40 bg-danger/5" : ""
                }`}
              >
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <SeverityBadge severity={incident.severity} />
                  <StateBadge state={incident.state} />
                </div>
                <Link
                  href={`/incidents/${incident.id}`}
                  className="rounded text-[14px] font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
                >
                  {incident.title}
                </Link>
                <p className="mt-1.5 flex flex-wrap items-center gap-2 text-[12px] text-muted">
                  <span>{incident.service_name}</span>
                  <span aria-hidden="true">·</span>
                  <span><LocalTime iso={incident.created_at} /></span>
                  <Link
                    href={`/replay/${incident.id}`}
                    className="ml-auto rounded transition-colors hover:text-fg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
                  >
                    replay →
                  </Link>
                </p>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
