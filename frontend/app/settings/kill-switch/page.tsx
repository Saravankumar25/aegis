"use client";

// Kill switch + circuit-breaker controls (FR-5.3). The server is the enforcement point
// (CLAUDE.md §12) — everything here is about making the state impossible to misread and
// making a refusal legible instead of silent. Red appears only where the meaning is
// genuinely "stop", which is the design system's one sanctioned use of colour.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, canAct, deniedReason, isAdmin, type User } from "@/lib/api";

interface BreakerStatus {
  tripped: boolean;
  open_trips: number;
  window_count: number;
}

export default function KillSwitchPage() {
  // `null` is "not known", which is distinct from "disengaged". There is no GET endpoint for
  // kill-switch state, so before any POST this page genuinely does not know — and claiming
  // "Disengaged" would be inventing the single most safety-relevant fact on the page.
  const [engaged, setEngaged] = useState<boolean | null>(null);
  const [breaker, setBreaker] = useState<BreakerStatus | null>(null);
  const [breakerError, setBreakerError] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [userResolved, setUserResolved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setBreakerError(false);
    api<BreakerStatus>("/circuit-breaker/status")
      .then((s) => {
        setBreaker(s);
        setBreakerError(false);
      })
      .catch(() => setBreakerError(true));

    api<User>("/auth/me")
      .then(setUser)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          window.location.href = "/login";
          return;
        }
        setUser(null);
      })
      .finally(() => setUserResolved(true));
  }, []);

  useEffect(load, [load]);

  function describe(err: unknown, fallback: string): string {
    if (err instanceof ApiError) {
      if (err.status === 403) return `Not permitted: ${err.message}`;
      if (err.status === 401) return "Your session expired. Sign in again.";
      return `${err.message} (${err.status})`;
    }
    return fallback;
  }

  async function setSwitch(next: boolean) {
    setError(null);
    setBusy(true);
    try {
      const result = await api<{ engaged: boolean }>("/kill-switch", {
        method: "POST",
        body: JSON.stringify({ engaged: next }),
      });
      setEngaged(result.engaged);
    } catch (err) {
      setError(describe(err, "Could not change the kill switch."));
    } finally {
      setBusy(false);
    }
  }

  async function clearBreaker() {
    setError(null);
    setBusy(true);
    try {
      setBreaker(await api<BreakerStatus>("/circuit-breaker/clear", { method: "POST" }));
    } catch (err) {
      setError(describe(err, "Could not clear the breaker."));
    } finally {
      setBusy(false);
    }
  }

  const engageDenied = deniedReason(user, "act");
  const adminDenied = deniedReason(user, "admin");
  // Until /auth/me resolves we do not know the role, so every privileged control stays
  // disabled. Enabling optimistically would show a viewer a live "Engage" button.
  const canEngage = userResolved && canAct(user);
  const canAdmin = userResolved && isAdmin(user);

  const secondary =
    "rounded-full border border-edge px-5 py-2 text-[13px] transition-colors hover:bg-surface2 disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg";

  return (
    <div className="max-w-2xl space-y-6">
      <div>
        <h1 className="display text-3xl">Safety</h1>
        <p className="mt-1.5 text-[13px] text-muted">
          Manual overrides for every autonomous action in the system.
        </p>
      </div>

      {userResolved && user && !canAct(user) && (
        <div className="rounded-xl border border-edge bg-surface px-4 py-3">
          <p className="text-[12.5px] text-muted">
            You are signed in as{" "}
            <span className="text-fg">{user.role.replace(/_/g, " ")}</span>. These controls are
            read-only for your role — the server rejects them regardless of what this page
            shows.
          </p>
        </div>
      )}

      {error && (
        <p role="alert" className="text-[13px] text-danger">
          {error}
        </p>
      )}

      <section
        className={`rounded-2xl border p-6 ${
          engaged ? "border-danger/50 bg-danger/5" : "border-edge bg-surface"
        }`}
      >
        <h2 className="text-[10px] uppercase tracking-[0.16em] text-muted">Kill switch</h2>
        <p className="mb-5 mt-2 text-[14px] leading-relaxed">
          {engaged === null
            ? "Current state is not reported by the API. Engaging halts all autonomous action across every in-flight incident, immediately."
            : engaged
              ? "Engaged — no autonomous action will execute anywhere, including work already approved."
              : "Disengaged — autonomous action is permitted within its safety gates."}
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setSwitch(true)}
            disabled={!canEngage || busy}
            aria-disabled={!canEngage || busy}
            title={engageDenied ?? "Halt all autonomous action"}
            className="rounded-full bg-danger px-5 py-2 text-[13px] font-semibold text-bg transition-opacity hover:opacity-85 disabled:cursor-not-allowed disabled:opacity-30 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
          >
            Engage kill switch
          </button>
          <button
            type="button"
            onClick={() => setSwitch(false)}
            disabled={!canAdmin || busy}
            aria-disabled={!canAdmin || busy}
            title={adminDenied ?? "Resume autonomous action"}
            className={secondary}
          >
            Disengage (admin)
          </button>
        </div>
        {engageDenied && <p className="mt-3 text-[11.5px] text-muted">{engageDenied}</p>}
      </section>

      <section className="rounded-2xl border border-edge bg-surface p-6">
        <h2 className="text-[10px] uppercase tracking-[0.16em] text-muted">
          Global circuit breaker
        </h2>
        {breakerError ? (
          <p className="mb-5 mt-2 text-[14px] text-warn">
            Breaker status unavailable.{" "}
            <button type="button" onClick={load} className="underline underline-offset-4">
              Retry
            </button>
          </p>
        ) : breaker === null ? (
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
          type="button"
          onClick={clearBreaker}
          disabled={!canAdmin || !breaker?.tripped || busy}
          aria-disabled={!canAdmin || !breaker?.tripped || busy}
          title={
            adminDenied ?? (breaker?.tripped ? "Clear the breaker" : "The breaker is not tripped")
          }
          className={secondary}
        >
          Clear breaker (admin)
        </button>
        {adminDenied && <p className="mt-3 text-[11.5px] text-muted">{adminDenied}</p>}
      </section>
    </div>
  );
}
