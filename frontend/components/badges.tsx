const SEVERITY_STYLES: Record<string, string> = {
  P1: "bg-red-500/15 text-red-400 border-red-500/40",
  P2: "bg-orange-500/15 text-orange-400 border-orange-500/40",
  P3: "bg-yellow-500/15 text-yellow-300 border-yellow-500/40",
  P4: "bg-slate-500/15 text-slate-300 border-slate-500/40",
};

const STATE_STYLES: Record<string, string> = {
  open: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  investigating: "bg-indigo-500/15 text-indigo-300 border-indigo-500/40",
  hypothesis_formed: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  monitoring: "bg-teal-500/15 text-teal-300 border-teal-500/40",
  resolved: "bg-green-500/15 text-green-300 border-green-500/40",
  closed: "bg-slate-500/15 text-slate-400 border-slate-500/40",
  reopened: "bg-rose-500/15 text-rose-300 border-rose-500/40",
};

const base =
  "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium";

export function SeverityBadge({ severity }: { severity: string }) {
  return (
    <span className={`${base} ${SEVERITY_STYLES[severity] ?? SEVERITY_STYLES.P4}`}>
      {severity}
    </span>
  );
}

export function StateBadge({ state }: { state: string }) {
  return (
    <span className={`${base} ${STATE_STYLES[state] ?? STATE_STYLES.closed}`}>
      {state.replace(/_/g, " ")}
    </span>
  );
}

export function AgentChip({ name }: { name: string }) {
  const colors: Record<string, string> = {
    triage: "text-sky-300",
    correlation: "text-violet-300",
    rca: "text-amber-300",
    observer: "text-emerald-300",
    resolution: "text-rose-300",
    communication: "text-teal-300",
  };
  return (
    <span className={`text-xs font-semibold uppercase ${colors[name] ?? "text-slate-300"}`}>
      {name}
    </span>
  );
}
