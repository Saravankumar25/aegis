"use client";

import { useEffect, useState } from "react";
import { api, type User } from "@/lib/api";

export function UserBadge() {
  const [user, setUser] = useState<User | null>(null);
  // Distinguishes "not signed in" from "we don't know yet", so the badge doesn't flash
  // "Sign in" at an authenticated user while /auth/me is still in flight.
  const [resolved, setResolved] = useState(false);

  useEffect(() => {
    api<User>("/auth/me")
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setResolved(true));
  }, []);

  async function signOut() {
    // The session lives in an httpOnly cookie, so only the server can end it.
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    window.location.assign("/login");
  }

  if (!resolved) return <span className="text-[11px] text-muted">…</span>;

  if (!user) {
    return (
      <a
        href="/login"
        className="whitespace-nowrap rounded text-[12px] text-muted transition-colors hover:text-fg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
      >
        Sign in
      </a>
    );
  }

  return (
    <span className="flex items-center gap-2.5">
      {user.photo_url && (
        // Google's avatar CDN, not a project asset; next/image would need a remotePatterns
        // entry per identity-provider host, for a 20px image it would never optimise.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={user.photo_url}
          alt=""
          width={20}
          height={20}
          className="rounded-full border border-edge"
          referrerPolicy="no-referrer"
        />
      )}
      <span className="hidden text-[11px] text-muted sm:inline">
        {user.display_name ?? user.email} · {user.role.replace(/_/g, " ")}
      </span>
      <button
        type="button"
        onClick={signOut}
        className="text-[11px] text-muted transition-colors hover:text-fg"
      >
        Sign out
      </button>
    </span>
  );
}
