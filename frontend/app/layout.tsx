import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/AppShell";

export const metadata: Metadata = {
  title: "Aegis — Autonomous incident response",
  description:
    "Seven agents that investigate your production incidents in minutes, cite every claim, and remediate only inside explicit safety limits.",
};

// Applied before first paint so the page never flashes the wrong theme.
//
// Mirrors lib/theme.ts `resolveTheme`: the stored value is a *preference* of
// dark | light | system, and `system` (or anything unrecognised, or no value at all) resolves
// against the OS query here rather than at hydration. Inlined rather than imported because it
// must run before React exists — the duplication is deliberate and the two must be changed
// together, which is why both sides name the same three values explicitly.
const THEME_SCRIPT = `(function(){try{var p=localStorage.getItem('aegis-theme');var t=(p==='dark'||p==='light')?p:(window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark');document.documentElement.dataset.theme=t;}catch(e){document.documentElement.dataset.theme='dark';}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // `data-scroll-behavior` tells Next the smooth scrolling in globals.css is intentional,
    // which silences its router warning about scroll restoration.
    <html lang="en" data-theme="dark" data-scroll-behavior="smooth" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="min-h-screen bg-bg text-fg">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
