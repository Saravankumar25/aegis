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
        <div className="mx-auto flex max-w-6xl items-center gap-x-4 gap-y-2 px-4 py-3 sm:gap-x-6 sm:px-6">
          <Link
            href="/"
            className="rounded text-[15px] font-semibold tracking-tight focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg"
          >
            Aegis
          </Link>
          <nav aria-label="Primary" className="flex gap-3 text-[13px] sm:gap-5">
            {NAV.map((item) => {
              const active = pathname.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={`whitespace-nowrap rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-fg ${
                    active ? "text-fg" : "text-muted transition-colors hover:text-fg"
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </nav>
          <div className="ml-auto flex shrink-0 items-center gap-3 sm:gap-4">
            <UserBadge />
            <ThemeToggle />
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">{children}</main>
    </>
  );
}
