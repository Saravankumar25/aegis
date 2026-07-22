"use client";

import { useCallback, useEffect, useState } from "react";
import {
  type ResolvedTheme,
  type ThemePreference,
  applyResolvedTheme,
  readPreference,
  resolveTheme,
  setPreference,
  watchSystemTheme,
} from "@/lib/theme";

export interface ThemeState {
  /** What the user chose: dark, light, or defer to the OS. */
  preference: ThemePreference;
  /** What is actually rendered right now. Differs from `preference` only under `system`. */
  resolved: ResolvedTheme;
  /** Null until the first client render, so SSR markup and hydration agree. */
  ready: boolean;
  choose: (preference: ThemePreference) => void;
}

/**
 * Theme preference as React state.
 *
 * `ready` exists because the preference lives in localStorage, which the server cannot read.
 * Rendering a toggle in its real position before hydration would either mismatch the server
 * markup or require guessing — so components hold off on the visual state one tick rather
 * than rendering something they may have to correct.
 */
export function useTheme(): ThemeState {
  const [preference, setPreferenceState] = useState<ThemePreference>("system");
  const [resolved, setResolved] = useState<ResolvedTheme>("dark");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const stored = readPreference();
    setPreferenceState(stored);
    setResolved(resolveTheme(stored));
    setReady(true);
  }, []);

  // Only subscribed while deferring to the OS. Under an explicit light/dark choice an OS
  // change must not move the app — that would silently override the user.
  useEffect(() => {
    if (preference !== "system") return;
    return watchSystemTheme((next) => {
      applyResolvedTheme(next);
      setResolved(next);
    });
  }, [preference]);

  const choose = useCallback((next: ThemePreference) => {
    setPreferenceState(next);
    setResolved(setPreference(next));
  }, []);

  return { preference, resolved, ready, choose };
}
