"use client";

// App chrome for the authenticated product surface. The marketing homepage
// ("/") renders its own navigation, so the shell steps aside there.

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ThemeToggle } from "@/components/ThemeToggle";
import { UserBadge } from "@/components/UserBadge";

const NAV = [
  { href: "/dashboard", label: "Incidents" },
  { href: "/approvals", label: "Approvals" },
  { href: "/settings/kill-switch", label: "Safety" },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  if (pathname === "/") return <>{children}</>;

  return (
    <>
      <header className="sticky top-0 z-40 border-b border-edge bg-bg/80 backdrop-blur-xl">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
          <Link href="/" className="text-[15px] font-semibold tracking-tight">
            Aegis
          </Link>
          <nav className="flex gap-5 text-[13px]">
            {NAV.map((item) => {
              const active = pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={active ? "text-fg" : "text-muted transition-colors hover:text-fg"}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto flex items-center gap-4">
            <UserBadge />
            <ThemeToggle />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">{children}</main>
    </>
  );
}
