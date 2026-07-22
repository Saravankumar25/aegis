"use client";

// SSE connection state. A dropped stream is shown as "reconnecting" with a manual retry
// rather than a silent "offline": the operator needs to know whether the quiet feed means
// nothing is happening or that they are looking at stale data.

import type { StreamStatus } from "@/lib/useEventStream";

export function LiveIndicator({
  status,
  onRetry,
  className = "",
}: {
  status: StreamStatus;
  onRetry: () => void;
  className?: string;
}) {
  if (status === "live") {
    return (
      <span
        className={`flex items-center gap-1.5 text-[11px] text-ok ${className}`}
        title="Receiving live updates"
      >
        <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-ok" />
        <span role="status">live</span>
      </span>
    );
  }

  if (status === "connecting") {
    return (
      <span className={`flex items-center gap-1.5 text-[11px] text-muted ${className}`}>
        <span aria-hidden="true" className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted" />
        <span role="status">connecting…</span>
      </span>
    );
  }

  return (
    <span className={`flex items-center gap-2 text-[11px] text-warn ${className}`}>
      <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-warn" />
      <span role="status">reconnecting — data may be stale</span>
      <button
        type="button"
        onClick={onRetry}
        className="rounded-full border border-edge px-2 py-0.5 text-[10.5px] text-muted transition-colors hover:bg-surface2 hover:text-fg"
      >
        retry now
      </button>
    </span>
  );
}
