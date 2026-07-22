"use client";

// Theme preference, shared by the header toggle and the settings page so the two cannot
// drift out of sync.
//
// Three preferences, two themes. `system` is not a third look — it is a *deferral*, and the
// distinction matters: a user on `system` who changes their OS appearance at dusk expects the
// app to follow without being touched. That means the preference and the resolved theme are
// different values and have to be stored and read differently, which is the whole reason this
// module exists rather than a boolean in a component.
//
// The resolved theme lands on `<html data-theme>`, which is what globals.css keys off. The
// pre-paint script in layout.tsx applies the same rule before first paint so the page never
// flashes the wrong one.

export type ThemePreference = "dark" | "light" | "system";
export type ResolvedTheme = "dark" | "light";

export const THEME_STORAGE_KEY = "aegis-theme";

const PREFERENCES: readonly ThemePreference[] = ["light", "dark", "system"] as const;

export function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === "string" && (PREFERENCES as readonly string[]).includes(value);
}

/** What the OS is currently asking for. Defaults to dark when the query is unsupported. */
export function systemTheme(): ResolvedTheme {
  if (typeof window === "undefined" || !window.matchMedia) return "dark";
  return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

/** The stored preference, or `system` when nothing has been chosen or storage is unreadable. */
export function readPreference(): ThemePreference {
  if (typeof window === "undefined") return "system";
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemePreference(stored) ? stored : "system";
  } catch {
    // Private mode or storage disabled. Not an error worth surfacing — the app still works,
    // it just cannot remember the choice past this tab.
    return "system";
  }
}

export function resolveTheme(preference: ThemePreference): ResolvedTheme {
  return preference === "system" ? systemTheme() : preference;
}

/** Write the resolved theme to the document. Safe to call repeatedly. */
export function applyResolvedTheme(theme: ResolvedTheme): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = theme;
}

/** Persist a preference and apply it immediately. Returns the theme actually applied. */
export function setPreference(preference: ThemePreference): ResolvedTheme {
  const resolved = resolveTheme(preference);
  applyResolvedTheme(resolved);
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // See readPreference: storage being unavailable must not break the switch itself.
  }
  return resolved;
}

/**
 * Follow OS appearance changes while the preference is `system`.
 *
 * Returns an unsubscribe function. Without this, choosing `system` would only sample the OS
 * once at load — the app would keep last night's dark theme through sunrise, which is
 * precisely the behaviour a user picks `system` to avoid.
 */
export function watchSystemTheme(onChange: (theme: ResolvedTheme) => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const query = window.matchMedia("(prefers-color-scheme: light)");
  const handler = (event: MediaQueryListEvent) => onChange(event.matches ? "light" : "dark");
  query.addEventListener("change", handler);
  return () => query.removeEventListener("change", handler);
}
