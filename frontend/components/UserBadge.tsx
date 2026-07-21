"use client";

import { useEffect, useState } from "react";
import { api, type User } from "@/lib/api";

export function UserBadge() {
  const [user, setUser] = useState<User | null>(null);

  useEffect(() => {
    api<User>("/auth/me")
      .then(setUser)
      .catch(() => setUser(null));
  }, []);

  if (!user) {
    return (
      <a href="/login" className="text-sm text-sky-400 hover:underline">
        Sign in
      </a>
    );
  }
  return (
    <span className="text-xs text-slate-400">
      {user.email} · <span className="uppercase">{user.role}</span>
    </span>
  );
}
