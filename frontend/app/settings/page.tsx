"use client";

// Account settings: who you are signed in as, what that role permits, and how the app looks.
//
// The role section is deliberately explicit about what the current role *cannot* do. An
// operator who finds a disabled Approve button mid-incident should be able to answer "why"
// here in one place, rather than inferring it from a greyed-out control at 3am.

import { useEffect, useState } from "react";
import { ApiError, ROLE_LABEL, api, canAct, isAdmin, type User } from "@/lib/api";
import { type ThemePreference } from "@/lib/theme";
import { useTheme } from "@/lib/useTheme";

const THEME_OPTIONS: { value: ThemePreference; label: string; hint: string }[] = [
  { value: "light", label: "Light", hint: "Always light, regardless of your device." },
  { value: "dark", label: "Dark", hint: "Always dark, regardless of your device." },
  {
    value: "system",
    label: "System",
    hint: "Follow your operating system, and change with it.",
  },
];

/** What each role can do, stated positively so the list reads as capability not restriction. */
const CAPABILITIES: { label: string; allowed: (u: User | null) => boolean }[] = [
  { label: "View incidents, evidence and agent reasoning", allowed: (u) => u !== null },
  { label: "Approve or reject proposed remediation", allowed: canAct },
  { label: "Resolve an incident", allowed: canAct },
  { label: "Engage the kill switch", allowed: canAct },
  { label: "Disengage the kill switch", allowed: isAdmin },
  { label: "Clear the global circuit breaker", allowed: isAdmin },
];

function Section({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-edge bg-surface p-5">
      <h2 className="text-[13px] font-semibold tracking-tight">{title}</h2>
      {description && <p className="mt-1 text-[12px] text-muted">{description}</p>}
      <div className="mt-4">{children}</div>
    </section>
  );
}

export default function SettingsPage() {
  const [user, setUser] = useState<User | null>(null);
  // Distinguishes "not signed in" from "still loading" — without it the page would flash
  // a signed-out state at an authenticated user.
  const [resolved, setResolved] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { preference, resolved: activeTheme, ready: themeReady, choose } = useTheme();

  useEffect(() => {
    api<User>("/auth/me")
      .then(setUser)
      .catch((err) => {
        if (err instanceof ApiError && err.status === 401) {
          window.location.href = "/login";
          return;
        }
        setError(err instanceof Error ? err.message : "could not load your profile");
      })
      .finally(() => setResolved(true));
  }, []);

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-[13px] text-muted">Your account and how Aegis looks.</p>
      </header>

      {error && (
        <div role="alert" className="rounded-lg border border-danger/40 bg-surface p-4">
          <p className="text-[13px] text-danger">{error}</p>
        </div>
      )}

      <Section title="Profile" description="Identity comes from your Google account.">
        {!resolved ? (
          <div className="space-y-2" aria-busy="true">
            <div className="h-4 w-40 animate-pulse rounded bg-surface2" />
            <div className="h-4 w-56 animate-pulse rounded bg-surface2" />
          </div>
        ) : !user ? (
          <p className="text-[13px] text-muted">
            You are not signed in.{" "}
            <a href="/login" className="text-fg underline underline-offset-2">
              Sign in
            </a>
            .
          </p>
        ) : (
          <div className="flex items-start gap-4">
            {user.photo_url ? (
              // Identity-provider CDN, not a project asset — see UserBadge for why this is
              // a plain <img>.
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={user.photo_url}
                alt=""
                width={44}
                height={44}
                className="rounded-full border border-edge"
                referrerPolicy="no-referrer"
              />
            ) : (
              <div
                aria-hidden="true"
                className="flex h-11 w-11 items-center justify-center rounded-full border border-edge bg-surface2 text-[15px] text-muted"
              >
                {(user.display_name ?? user.email).charAt(0).toUpperCase()}
              </div>
            )}
            <dl className="min-w-0 flex-1 space-y-2 text-[13px]">
              <div className="flex flex-wrap items-baseline gap-x-3">
                <dt className="w-16 shrink-0 text-[11px] uppercase tracking-wider text-muted">
                  Name
                </dt>
                <dd className="min-w-0 break-words">{user.display_name ?? "—"}</dd>
              </div>
              <div className="flex flex-wrap items-baseline gap-x-3">
                <dt className="w-16 shrink-0 text-[11px] uppercase tracking-wider text-muted">
                  Email
                </dt>
                <dd className="min-w-0 break-all">{user.email}</dd>
              </div>
              <div className="flex flex-wrap items-baseline gap-x-3">
                <dt className="w-16 shrink-0 text-[11px] uppercase tracking-wider text-muted">
                  Role
                </dt>
                <dd>
                  <span className="inline-flex items-center rounded-full border border-edge px-2 py-0.5 text-[11px]">
                    {ROLE_LABEL[user.role] ?? user.role}
                  </span>
                </dd>
              </div>
            </dl>
          </div>
        )}
      </Section>

      {resolved && user && (
        <Section
          title="Permissions"
          description="Roles are granted by an operator allowlist, not self-service. Every action is re-checked on the server."
        >
          <ul className="space-y-2">
            {CAPABILITIES.map((cap) => {
              const allowed = cap.allowed(user);
              return (
                <li key={cap.label} className="flex items-start gap-2.5 text-[13px]">
                  <span
                    aria-hidden="true"
                    className={allowed ? "text-ok" : "text-muted"}
                  >
                    {allowed ? "✓" : "—"}
                  </span>
                  <span className={allowed ? "" : "text-muted"}>
                    {cap.label}
                    <span className="sr-only">{allowed ? " (permitted)" : " (not permitted)"}</span>
                  </span>
                </li>
              );
            })}
          </ul>
          {!canAct(user) && (
            <p className="mt-4 border-t border-edge pt-3 text-[12px] text-muted">
              To approve remediation or use the kill switch, an administrator must add your
              email to the on-call or admin allowlist.
            </p>
          )}
        </Section>
      )}

      <Section title="Appearance">
        <fieldset>
          <legend className="sr-only">Theme</legend>
          <div className="grid gap-2 sm:grid-cols-3">
            {THEME_OPTIONS.map((option) => {
              const selected = themeReady && preference === option.value;
              return (
                <label
                  key={option.value}
                  className={`cursor-pointer rounded-lg border p-3 transition-colors ${
                    selected
                      ? "border-fg bg-surface2"
                      : "border-edge hover:border-muted"
                  }`}
                >
                  <span className="flex items-center gap-2">
                    <input
                      type="radio"
                      name="theme"
                      value={option.value}
                      checked={selected}
                      onChange={() => choose(option.value)}
                      className="h-3.5 w-3.5 accent-fg"
                    />
                    <span className="text-[13px] font-medium">{option.label}</span>
                  </span>
                  <span className="mt-1.5 block text-[11px] leading-snug text-muted">
                    {option.hint}
                  </span>
                </label>
              );
            })}
          </div>
        </fieldset>
        {themeReady && preference === "system" && (
          <p className="mt-3 text-[12px] text-muted">
            Currently showing <strong className="font-medium text-fg">{activeTheme}</strong>{" "}
            because that is what your device is set to.
          </p>
        )}
      </Section>

      <Section
        title="Safety controls"
        description="The kill switch and circuit breaker live on their own page — they affect every in-flight incident, not just your session."
      >
        <a
          href="/settings/kill-switch"
          className="inline-flex items-center gap-1.5 rounded-lg border border-edge px-3 py-2 text-[13px] transition-colors hover:border-muted focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
        >
          Open safety controls
          <span aria-hidden="true">→</span>
        </a>
      </Section>
    </div>
  );
}
