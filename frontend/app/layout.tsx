import type { Metadata } from "next";
import "./globals.css";
import { AppShell } from "@/components/AppShell";

export const metadata: Metadata = {
  title: "Aegis — Autonomous incident response",
  description:
    "Seven agents that investigate your production incidents in minutes, cite every claim, and remediate only inside explicit safety limits.",
};

// Applied before first paint so the page never flashes the wrong theme.
const THEME_SCRIPT = `(function(){try{var t=localStorage.getItem('aegis-theme');if(!t){t=window.matchMedia('(prefers-color-scheme: light)').matches?'light':'dark';}document.documentElement.dataset.theme=t;}catch(e){document.documentElement.dataset.theme='dark';}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className="min-h-screen bg-bg text-fg">
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
