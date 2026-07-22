"use client";

// Tier-2 approval queue (Journey B): full reasoning + blast radius + the documented
// undo, not just a conclusion. Role gating here is cosmetic; the server enforces it.

import { useCallback, useEffect, useState } from "react";
import { LocalTime } from "@/components/LocalTime";
import { SeverityBadge } from "@/components/badges";
import { api, ApiError, canAct, deniedReason, type User } from "@/lib/api";

interface ActionOut {
  id: string;
  incident_id: string;
  tier: number;
  action_type: string;
  target_resource_id: string;
  status: string;
  reasoning: string;
  blast_radius: { dependents?: string[]; count?: number };
  compensating_action: { note?: string };
  shadow: boolean;
  expires_at: string;
}

interface PendingApproval {
  action: ActionOut;
  incident_title: string;
  service_name: string;
  severity: "P1" | "P2" | "P3" | "P4";
}

export default function ApprovalsPage() {
  const [items, setItems] = useState<PendingApproval[] | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [userResolved, setUserResolved] = useState(false);

  const load = useCallback(() => {
    api<PendingApproval[]>("/approvals")
      .then((rows) => {
        setItems(rows);
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
    api<User>("/auth/me")
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setUserResolved(true));
  }, [load]);

  // Until the role resolves, the buttons stay disabled: enabling them optimistically would
  // briefly offer a viewer an approval control the server will refuse.
  const canDecide = userResolved && canAct(user);
  const denied = deniedReason(user, "act");

  async function decide(item: PendingApproval, decision: "approved" | "rejected") {
    setBusy(item.action.id);
    setError(null);
    try {
      await api(`/incidents/${item.action.incident_id}/approvals`, {
        method: "POST",
        body: JSON.stringify({ action_id: item.action.id, decision }),
      });
      load();
    } catch (err) {
      // A refused approval must read as a refusal, not as a click that did nothing.
      setError(
        err instanceof ApiError
          ? err.status === 403
            ? `Not permitted: ${err.message}`
            : `${err.message} (${err.status})`
          : "The decision could not be recorded.",
      );
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <h1 className="display text-3xl">Approvals</h1>
      <p className="mb-7 mt-1.5 text-[13px] text-muted">
        Tier-2 proposals waiting on a human. Nothing here has touched infrastructure.
      </p>

      {userResolved && user && !canAct(user) && (
        <div className="mb-4 rounded-xl border border-edge bg-surface px-4 py-3">
          <p className="text-[12.5px] text-muted">{denied} You can review proposals here.</p>
        </div>
      )}

      {error && (
        <p role="alert" className="mb-4 text-[13px] text-danger">
          {error}
        </p>
      )}

      {items === null ? (
        <div className="space-y-3" aria-busy="true" aria-live="polite">
          <span className="sr-only">Loading approvals…</span>
          {[0, 1].map((i) => (
            <div key={i} className="h-40 animate-pulse rounded-2xl bg-surface2" />
          ))}
        </div>
      ) : items.length === 0 ? (
        <div className="rounded-2xl border border-edge bg-surface px-6 py-16 text-center">
          <p className="text-[15px]">Nothing waiting</p>
          <p className="mt-1.5 text-[13px] text-muted">
            Proposals appear here when a fix needs your approval before it runs.
          </p>
        </div>
      ) : (
        <ul className="space-y-4">
          {items.map((item) => (
            <li key={item.action.id} className="rounded-2xl border border-edge bg-surface p-6">
              <div className="mb-3 flex flex-wrap items-center gap-3">
                <SeverityBadge severity={item.severity} />
                <span className="font-medium">{item.incident_title}</span>
                <span className="text-[11px] text-muted">
                  expires <LocalTime iso={item.action.expires_at} />
                </span>
              </div>

              <p className="text-[14px]">
                Proposed{" "}
                <code className="rounded border border-edge bg-bg px-1.5 py-0.5 text-[12.5px]">
                  {item.action.action_type}
                </code>{" "}
                on <code className="text-muted">{item.action.target_resource_id}</code> · tier{" "}
                {item.action.tier}
              </p>

              <p className="mt-3 whitespace-pre-wrap text-[12.5px] leading-relaxed text-muted">
                {item.action.reasoning}
              </p>

              <div className="mt-4 grid gap-3 rounded-xl border border-edge bg-bg p-4 text-[12px] sm:grid-cols-2">
                <div>
                  <p className="text-[10px] uppercase tracking-[0.14em] text-muted">
                    Blast radius
                  </p>
                  <p className="mt-1">
                    {item.action.blast_radius.count ?? 0} dependent service
                    {(item.action.blast_radius.count ?? 0) === 1 ? "" : "s"}
                    {item.action.blast_radius.dependents?.length
                      ? ` · ${item.action.blast_radius.dependents.join(", ")}`
                      : ""}
                  </p>
                </div>
                <div>
                  <p className="text-[10px] uppercase tracking-[0.14em] text-muted">If we undo</p>
                  <p className="mt-1">{item.action.compensating_action.note ?? "documented"}</p>
                </div>
              </div>

              <div className="mt-5 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => decide(item, "approved")}
                  disabled={!canDecide || busy === item.action.id}
                  aria-disabled={!canDecide || busy === item.action.id}
                  title={denied ?? "Approve and execute this action"}
                  className="rounded-full bg-inverse-bg px-5 py-2 text-[13px] font-medium text-inverse-fg transition-opacity hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
                >
                  {busy === item.action.id ? "Working…" : "Approve"}
                </button>
                <button
                  type="button"
                  onClick={() => decide(item, "rejected")}
                  disabled={!canDecide || busy === item.action.id}
                  aria-disabled={!canDecide || busy === item.action.id}
                  title={denied ?? "Reject this proposal"}
                  className="rounded-full border border-edge px-5 py-2 text-[13px] font-medium transition-colors hover:bg-surface2 disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
                >
                  Reject
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
