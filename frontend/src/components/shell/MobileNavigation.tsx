'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import * as Dialog from '@radix-ui/react-dialog';
import { VisuallyHidden } from '@radix-ui/react-visually-hidden';
import { X } from 'lucide-react';
import { cn } from '@/lib/utils';
import { AegisMark } from './AegisMark';
import { NAV_SECTIONS } from './navigation';

export function MobileNavigation({ open, onClose }: { open: boolean; onClose: () => void }) {
  const pathname = usePathname();

  return (
    <Dialog.Root open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-canvas/80 lg:hidden" />
        <Dialog.Content className="fixed inset-y-0 left-0 z-50 w-[min(88vw,320px)] border-r border-hairline bg-canvas outline-none lg:hidden">
          <VisuallyHidden><Dialog.Title>Primary navigation</Dialog.Title></VisuallyHidden>
          <VisuallyHidden><Dialog.Description>Navigate between Aegis console areas.</Dialog.Description></VisuallyHidden>
          <nav aria-label="Primary" className="flex h-full flex-col">
            <div className="flex h-[54px] items-center gap-2.5 border-b border-hairline px-4">
              <AegisMark className="h-5 w-5" />
              <span className="text-body font-semibold">Aegis</span>
              <Dialog.Close asChild>
                <button
                  type="button"
                  aria-label="Close navigation"
                  className="ml-auto rounded-btn p-2 text-ink-secondary hover:bg-surface-2"
                >
                  <X className="h-4 w-4" aria-hidden />
                </button>
              </Dialog.Close>
            </div>
            <div className="flex-1 overflow-y-auto py-3">
              {NAV_SECTIONS.map((section) => (
                <section key={section.label} aria-labelledby={`mobile-nav-${section.label}`} className="mb-4">
                  <h2 id={`mobile-nav-${section.label}`} className="px-4 pb-1.5 text-meta uppercase tracking-wider text-ink-tertiary">
                    {section.label}
                  </h2>
                  <ul className="space-y-0.5 px-2">
                    {section.items.map((item) => {
                      const active = pathname.startsWith(item.href);
                      const Icon = item.icon;
                      return (
                        <li key={item.href}>
                          <Link
                            href={item.href}
                            onClick={onClose}
                            aria-current={active ? 'page' : undefined}
                            className={cn(
                              'flex min-h-11 items-center gap-3 rounded-btn px-3 text-body',
                              active ? 'bg-surface-3 text-ink-primary' : 'text-ink-secondary hover:bg-surface-2',
                            )}
                          >
                            <Icon className="h-4 w-4" aria-hidden />
                            {item.label}
                          </Link>
                        </li>
                      );
                    })}
                  </ul>
                </section>
              ))}
            </div>
          </nav>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
