"use client";

import { useState } from "react";
import { api, ApiError } from "@/lib/api";

export default function LoginPage() {
  const [email, setEmail] = useState("oncall@aegis.dev");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      // Full navigation, not router.push: the shell's UserBadge reads /auth/me on
      // mount, so a client-side transition would leave it showing "Sign in".
      window.location.assign("/dashboard");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "login failed");
    } finally {
      setBusy(false);
    }
  }

  const field =
    "w-full rounded-lg border border-edge bg-bg px-3.5 py-2.5 text-[14px] outline-none transition-colors placeholder:text-muted focus:border-fg/40";

  return (
    <div className="mx-auto mt-24 max-w-sm">
      <h1 className="display mb-1 text-2xl">Sign in</h1>
      <p className="mb-7 text-[13px] text-muted">Aegis incident response</p>
      <form onSubmit={submit} className="space-y-3">
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="email"
          className={field}
        />
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="password"
          className={field}
        />
        {error && <p className="text-[13px] text-danger">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-inverse-bg px-3 py-2.5 text-[14px] font-medium text-inverse-fg transition-opacity hover:opacity-80 disabled:opacity-40"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
