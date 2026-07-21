"use client";

// Kill switch + circuit-breaker controls (FR-5.3). Server enforces roles; this page
// just makes the state impossible to misread. Red appears only where the meaning is
// genuinely "stop" — it is the one place a colour carries information.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, type User } from "@/lib/api";

interface BreakerStatus {
  tripped: boolean;
  open_trips: number;
  window_count: number;
}

export default function KillSwitchPage() {
  const [engaged, setEngaged] = useState<boolean | null>(null);
  const [breaker, setBreaker] = useState<BreakerStatus | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api<BreakerStatus>("/circuit-breaker/status").then(setBreaker).catch(() => null);
    api<User>("/auth/me")
      .then(setUser)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) window.location.href = "/login";
      });
  }, []);

  useEffect(load, [load]);

  async function setSwitch(next: boolean) {
    setError(null);
    try {
      const result = await api<{ engaged: boolean }>("/kill-switch", {
        method: "POST",
        body: JSON.stringify({ engaged: next }),
      });
      setEngaged(result.engaged);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed");
    }
  }

  async function clearBreaker() {
    setError(null);
    try {
      const result = await api<BreakerStatus>("/circuit-breaker/clear", { method: "POST" });
      setBreaker(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed");
    }
  }

  const canEngage = user?.role === "on_call_engineer" || user?.role === "admin";
  const isAdmin = user?.role === "admin";
  const secondary =
    "rounded-full border border-edge px-5 py-2 text-[13px] transition-colors hover:bg-surface2 disabled:opacity-30";

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <h1 className="display text-3xl">Safety</h1>
        <p className="mt-1.5 text-[13px] text-muted">
          Manual overrides for every autonomous action in the system.
        </p>
      </div>

      {error && <p className="text-[13px] text-danger">{error}</p>}

      <section
        className={`rounded-2xl border p-6 ${
          engaged ? "border-danger/50 bg-danger/5" : "border-edge bg-surface"
        }`}
      >
        <h2 className="text-[10px] uppercase tracking-[0.16em] text-muted">Kill switch</h2>
        <p className="mb-5 mt-2 text-[14px] leading-relaxed">
          {engaged === null
            ? "Engaging halts all autonomous action across every in-flight incident, immediately."
            : engaged
              ? "Engaged — no autonomous action will execute anywhere, including work already approved."
              : "Disengaged — autonomous action is permitted within its safety gates."}
        </p>
        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => setSwitch(true)}
            disabled={!canEngage}
            className="rounded-full bg-danger px-5 py-2 text-[13px] font-semibold text-bg transition-opacity hover:opacity-85 disabled:opacity-30"
          >
            Engage kill switch
          </button>
          <button
            onClick={() => setSwitch(false)}
            disabled={!isAdmin}
            title={isAdmin ? "" : "admin only"}
            className={secondary}
          >
            Disengage (admin)
          </button>
        </div>
      </section>

      <section className="rounded-2xl border border-edge bg-surface p-6">
        <h2 className="text-[10px] uppercase tracking-[0.16em] text-muted">
          Global circuit breaker
        </h2>
        {breaker === null ? (
          <p className="mb-5 mt-2 text-[14px] text-muted">Loading…</p>
        ) : (
          <p className="mb-5 mt-2 text-[14px] leading-relaxed">
            {breaker.tripped ? (
              <span className="text-danger">
                Tripped — Tier-1 auto-execution is suspended system-wide until cleared.
              </span>
            ) : (
              <span className="text-ok">Closed — normal operation.</span>
            )}
          </p>
        )}
        <button
          onClick={clearBreaker}
          disabled={!isAdmin || !breaker?.tripped}
          title={isAdmin ? "" : "admin only"}
          className={secondary}
        >
          Clear breaker (admin)
        </button>
      </section>
    </div>
  );
}
