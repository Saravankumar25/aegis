"use client";

// Black ⇄ white theme switch. The choice is written to <html data-theme> and
// localStorage; the inline script in layout.tsx applies it before first paint so
// there is no flash of the wrong theme.

import { useEffect, useState } from "react";

type Theme = "dark" | "light";

export function ThemeToggle({ className = "" }: { className?: string }) {
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    const current = (document.documentElement.dataset.theme as Theme) ?? "dark";
    setTheme(current);
  }, []);

  function apply(next: Theme) {
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("aegis-theme", next);
    } catch {
      // Private mode / storage disabled: the toggle still works for this session.
    }
    setTheme(next);
  }

  const isDark = theme !== "light";

  return (
    <button
      type="button"
      role="switch"
      aria-checked={isDark}
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
      onClick={() => apply(isDark ? "light" : "dark")}
      className={`group relative inline-flex h-7 w-[52px] shrink-0 items-center rounded-full border border-edge bg-surface2 transition-colors ${className}`}
    >
      <span
        className={`absolute flex h-5 w-5 items-center justify-center rounded-full bg-inverse-bg text-[10px] text-inverse-fg transition-transform duration-300 ease-out ${
          isDark ? "translate-x-[3px]" : "translate-x-[26px]"
        }`}
      >
        {isDark ? "●" : "○"}
      </span>
      <span className="sr-only">{isDark ? "Dark" : "Light"}</span>
    </button>
  );
}
