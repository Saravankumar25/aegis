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
      <a href="/login" className="text-[12px] text-muted transition-colors hover:text-fg">
        Sign in
      </a>
    );
  }
  return (
    <span className="hidden text-[11px] text-muted sm:inline">
      {user.email} · {user.role.replace(/_/g, " ")}
    </span>
  );
}
