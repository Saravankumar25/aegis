"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";
import { describeSignInError, endFirebaseSession, signInWithGoogle } from "@/lib/firebase";

export default function LoginPage() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function signIn() {
    setBusy(true);
    setError(null);
    try {
      // 1. Prove the Google identity in the browser.
      const idToken = await signInWithGoogle();
      try {
        // 2. Exchange it for Aegis's httpOnly session cookie. This is the only moment the
        //    ID token exists in this app.
        await api("/auth/session", {
          method: "POST",
          body: JSON.stringify({ id_token: idToken }),
        });
      } finally {
        // 3. Drop the Firebase session whether or not the exchange succeeded, so a failed
        //    login never leaves a live token sitting in the tab (CLAUDE.md §12).
        await endFirebaseSession();
      }
      // Full navigation, not router.push: the shell's UserBadge reads /auth/me on mount,
      // so a client-side transition would leave it showing "Sign in".
      window.location.assign("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : describeSignInError(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto mt-24 max-w-sm">
      <h1 className="display mb-1 text-2xl">Sign in</h1>
      <p className="mb-7 text-[13px] text-muted">Aegis incident response</p>

      <button
        type="button"
        onClick={signIn}
        disabled={busy}
        className="flex w-full items-center justify-center gap-2.5 rounded-lg border border-edge bg-bg px-3 py-2.5 text-[14px] font-medium transition-colors hover:border-fg/40 disabled:opacity-40"
      >
        <GoogleMark />
        {busy ? "Signing in…" : "Continue with Google"}
      </button>

      {error && (
        <p role="alert" className="mt-3 text-[13px] text-danger">
          {error}
        </p>
      )}

      <p className="mt-7 text-[12px] leading-relaxed text-muted">
        Anyone with a Google account can sign in. Permission to approve a remediation is
        granted separately by an operator, so a new account starts read-only.
      </p>
    </div>
  );
}

// Google's mark keeps its brand colours deliberately: the design system is monochrome, but a
// recoloured third-party logo misrepresents someone else's brand (and their guidelines
// forbid it). This is the documented exception, not a drift from the token set.
function GoogleMark() {
  return (
    <svg width="16" height="16" viewBox="0 0 18 18" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.91c1.7-1.57 2.69-3.88 2.69-6.62z"
      />
      <path
        fill="#34A853"
        d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.91-2.26c-.81.54-1.84.86-3.05.86-2.35 0-4.34-1.58-5.05-3.71H.96v2.33A9 9 0 0 0 9 18z"
      />
      <path
        fill="#FBBC05"
        d="M3.95 10.71a5.41 5.41 0 0 1 0-3.42V4.96H.96a9 9 0 0 0 0 8.08l2.99-2.33z"
      />
      <path
        fill="#EA4335"
        d="M9 3.58c1.32 0 2.51.45 3.44 1.35l2.58-2.59C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.96l2.99 2.33C4.66 5.16 6.65 3.58 9 3.58z"
      />
    </svg>
  );
}
