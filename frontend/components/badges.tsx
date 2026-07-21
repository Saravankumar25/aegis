// Monochrome chips. The only chromatic values are the severity ramp — an
// incident tool that renders P1 and P4 identically has lost information the
// operator needs at a glance. Everything else differentiates by weight and a
// small state dot, not by hue.

const base =
  "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium";

const SEVERITY: Record<string, string> = {
  P1: "border-sev1/40 text-sev1",
  P2: "border-sev2/40 text-sev2",
  P3: "border-sev3/40 text-sev3",
  P4: "border-edge text-sev4",
};

export function SeverityBadge({ severity }: { severity: string }) {
  return (
    <span className={`${base} ${SEVERITY[severity] ?? SEVERITY.P4}`}>
      <span className="h-1.5 w-1.5 rounded-full bg-current" />
      {severity}
    </span>
  );
}

// Terminal/settled states read quieter than in-flight ones.
const ACTIVE_STATES = new Set([
  "open",
  "investigating",
  "hypothesis_formed",
  "remediation_proposed",
  "remediation_approved",
  "remediation_executed",
  "monitoring",
  "reopened",
]);

export function StateBadge({ state }: { state: string }) {
  const active = ACTIVE_STATES.has(state);
  return (
    <span
      className={`${base} border-edge ${active ? "bg-surface2 text-fg" : "text-muted"}`}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${active ? "bg-fg" : "bg-muted"}`}
      />
      {state.replace(/_/g, " ")}
    </span>
  );
}

export function AgentChip({ name }: { name: string }) {
  return (
    <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
      {name}
    </span>
  );
}
