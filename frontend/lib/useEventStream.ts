"use client";

// Shared SSE subscription (ESD §5: push, never poll).
//
// EventSource reconnects on its own, but its built-in retry is opaque: a dropped API gives
// no signal to the operator, and a stream that fails to re-establish looks identical to one
// that is merely quiet. This owns the lifecycle explicitly so the UI can say which of the
// two is happening, and so every timer and listener is torn down on unmount.

import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE } from "@/lib/api";

export type StreamStatus = "connecting" | "live" | "reconnecting";

const BASE_DELAY_MS = 1_000;
const MAX_DELAY_MS = 30_000;

export function useEventStream(
  path: string | null,
  eventTypes: readonly string[],
  onEvent: () => void,
): { status: StreamStatus; retryNow: () => void } {
  const [status, setStatus] = useState<StreamStatus>("connecting");

  // Refs so reconnect scheduling never re-runs the effect (which would tear down the very
  // connection it just made) and the handler is never a stale closure.
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const typesRef = useRef(eventTypes);
  typesRef.current = eventTypes;

  const sourceRef = useRef<EventSource | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const cancelledRef = useRef(false);

  const connect = useCallback(() => {
    if (cancelledRef.current || path === null) return;

    sourceRef.current?.close();
    const source = new EventSource(`${API_BASE}${path}`, { withCredentials: true });
    sourceRef.current = source;

    source.onopen = () => {
      attemptRef.current = 0;
      setStatus("live");
    };

    const handler = () => onEventRef.current();
    for (const type of typesRef.current) source.addEventListener(type, handler);

    source.onerror = () => {
      // Close before scheduling: leaving it open would race the browser's own retry and
      // produce two live streams delivering duplicate events.
      source.close();
      if (cancelledRef.current) return;
      setStatus("reconnecting");
      const delay = Math.min(BASE_DELAY_MS * 2 ** attemptRef.current, MAX_DELAY_MS);
      attemptRef.current += 1;
      timerRef.current = setTimeout(connect, delay);
    };
  }, [path]);

  const retryNow = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    attemptRef.current = 0;
    setStatus("connecting");
    connect();
  }, [connect]);

  useEffect(() => {
    cancelledRef.current = false;
    attemptRef.current = 0;
    setStatus("connecting");
    connect();
    return () => {
      cancelledRef.current = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      sourceRef.current?.close();
      sourceRef.current = null;
    };
  }, [connect]);

  return { status, retryNow };
}
