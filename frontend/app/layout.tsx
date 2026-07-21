import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import { UserBadge } from "@/components/UserBadge";

export const metadata: Metadata = {
  title: "Aegis — Incident Response",
  description: "Multi-agent incident response for Meridian Commerce",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-edge bg-panel/80 backdrop-blur">
          <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
            <Link href="/dashboard" className="text-lg font-semibold tracking-tight">
              <span className="text-sky-400">Aegis</span>{" "}
              <span className="text-slate-400 text-sm">incident response</span>
            </Link>
            <nav className="flex gap-4 text-sm text-slate-300">
              <Link href="/dashboard" className="hover:text-white">
                Dashboard
              </Link>
            </nav>
            <div className="ml-auto">
              <UserBadge />
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-6xl px-6 py-6">{children}</main>
      </body>
    </html>
  );
}
