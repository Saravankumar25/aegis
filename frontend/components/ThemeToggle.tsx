"use client";

// Header theme control. Cycles light → dark → system.
//
// A two-position switch cannot express "follow the OS", and that is the preference most
// users actually want — so this is a cycling button rather than a toggle. The labelled
// picker lives in /settings; this is the quick control.

import { type ThemePreference } from "@/lib/theme";
import { useTheme } from "@/lib/useTheme";

const ORDER: readonly ThemePreference[] = ["light", "dark", "system"] as const;

const GLYPH: Record<ThemePreference, string> = {
  light: "○",
  dark: "●",
  // Deliberately not a sun/moon: under `system` the app is not asserting a look, it is
  // deferring, and a half-filled circle reads as "whatever it is out there".
  system: "◐",
};

const LABEL: Record<ThemePreference, string> = {
  light: "Light",
  dark: "Dark",
  system: "System",
};

export function ThemeToggle({ className = "" }: { className?: string }) {
  const { preference, resolved, ready, choose } = useTheme();

  // Before hydration the stored preference is unknown; rendering a definite state would
  // risk showing the wrong one for a frame.
  const shown: ThemePreference = ready ? preference : "system";
  const next = ORDER[(ORDER.indexOf(shown) + 1) % ORDER.length];

  return (
    <button
      type="button"
      onClick={() => choose(next)}
      title={`Theme: ${LABEL[shown]}${
        shown === "system" ? ` (currently ${resolved})` : ""
      } — click for ${LABEL[next]}`}
      aria-label={`Theme: ${LABEL[shown]}. Switch to ${LABEL[next]}.`}
      className={`inline-flex h-7 shrink-0 items-center gap-1.5 rounded-full border border-edge bg-surface2 px-2.5 text-[11px] text-muted transition-colors hover:text-fg focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg ${className}`}
    >
      <span aria-hidden="true">{GLYPH[shown]}</span>
      <span className="hidden sm:inline">{LABEL[shown]}</span>
    </button>
  );
}
