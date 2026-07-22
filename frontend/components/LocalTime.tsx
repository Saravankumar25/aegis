"use client";

// Timestamps rendered without a hydration mismatch.
//
// `toLocaleTimeString()` resolves against the *runtime's* locale and timezone, so the server
// and the browser produce different strings for the same instant and React discards the tree
// with a recoverable-error warning. Every timestamp in the app hit this.
//
// The fix is to render something deterministic on the server — UTC, explicitly labelled —
// and upgrade to the operator's local formatting after mount. Both passes agree, so there is
// no mismatch, and the operator still ends up reading local time.

import { useEffect, useState } from "react";

function utcLabel(iso: string, withDate: boolean): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const time = d.toISOString().slice(11, 19);
  return withDate ? `${d.toISOString().slice(0, 10)} ${time} UTC` : `${time} UTC`;
}

export function LocalTime({
  iso,
  withDate = false,
}: {
  iso: string;
  withDate?: boolean;
}) {
  const [text, setText] = useState(() => utcLabel(iso, withDate));

  useEffect(() => {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return;
    setText(withDate ? d.toLocaleString() : d.toLocaleTimeString());
  }, [iso, withDate]);

  // `dateTime` keeps the machine-readable instant available regardless of formatting.
  return <time dateTime={iso}>{text}</time>;
}
