"use client";

// The explainability surface (ESD §5, Phase 7). This is how an on-call engineer decides
// whether to trust the investigation, so the layout is opinionated about what must never be
// hidden:
//
//   * the headline is the scannable unit — one line per agent, readable without expanding;
//   * `uncertainty` renders in the *collapsed* header, not behind the expander. Uncertainty
//     that costs a click reads as absent, and a timeline of confident headlines with the
//     caveats folded away is exactly the failure this view exists to prevent;
//   * `alternatives_considered` is always visible for the same reason — an agent that
//     appears to have considered one option reads as more certain than it is;
//   * model / tokens / latency are present but deliberately subordinate: they explain cost,
//     not correctness, and they must never outrank the reasoning visually.
//
// Colour follows the V1.5d rule: greyscale everywhere except observer validation (ok) and
// refusal//danger, which carry information greyscale cannot.

import { useState } from "react";
import { LocalTime } from "@/components/LocalTime";
import type { AgentExplanation, AgentStep, Citation } from "@/lib/api";

/** Deterministic thousands grouping — `toLocaleString()` would differ server vs client. */
function groupDigits(n: number): string {
  return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Agents whose card is expanded on load: the conclusions an engineer opens the page for. */
const EXPANDED_BY_DEFAULT = new Set(["rca", "observer", "resolution"]);

function formatLatency(ms: number | null): string | null {
  if (ms === null) return null;
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${ms}ms`;
}

function formatCost(usd: number | null): string | null {
  if (usd === null) return null;
  // Free-tier models report a real 0.00 — render it as "$0.00" rather than hiding the row,
  // because "no cost recorded" and "cost was zero" are different facts.
  return `$${usd.toFixed(4)}`;
}

function ConfidenceMeter({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  return (
    <span
      className="inline-flex items-center gap-1.5"
      title={`Model-reported confidence: ${pct}%`}
    >
      <span
        className="flex h-1.5 w-12 overflow-hidden rounded-full bg-edge"
        role="img"
        aria-label={`Confidence ${pct} percent`}
      >
        <span className="h-full rounded-full bg-fg" style={{ width: `${pct}%` }} />
      </span>
      <span className="font-mono text-[11px] text-muted">{pct}%</span>
    </span>
  );
}

function Section({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <h4 className="mb-1.5 text-[10px] uppercase tracking-[0.16em] text-muted">{label}</h4>
      {children}
    </div>
  );
}

function BulletList({ items }: { items: string[] }) {
  return (
    <ul className="space-y-1.5">
      {items.map((item, i) => (
        <li key={i} className="flex gap-2 text-[13px] leading-relaxed">
          <span aria-hidden="true" className="select-none text-muted">
            ·
          </span>
          <span>{item}</span>
        </li>
      ))}
    </ul>
  );
}

function CitationList({ citations }: { citations: Citation[] }) {
  return (
    <ul className="space-y-2">
      {citations.map((citation) => (
        <li key={citation.id} className="rounded-xl border border-edge bg-bg p-3.5">
          <div className="mb-2 flex flex-wrap items-center gap-2 text-[10.5px] text-muted">
            <span className="rounded border border-edge px-1.5 py-0.5 uppercase tracking-wide">
              {citation.evidence_type}
            </span>
            <code className="break-all">{citation.evidence_ref}</code>
            {citation.validated_by_observer ? (
              <span className="flex items-center gap-1 text-ok">
                <span aria-hidden="true">✓</span> observer-validated
              </span>
            ) : (
              // Absence of validation is a fact worth stating. Rendering nothing would let
              // an unvalidated citation pass as a validated one at a glance.
              <span className="flex items-center gap-1 text-warn">
                <span aria-hidden="true">•</span> not validated
              </span>
            )}
          </div>
          {citation.evidence_snippet_redacted && (
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[11.5px] leading-relaxed text-fg/80">
              {citation.evidence_snippet_redacted}
            </pre>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Resolution-specific fields, which live in `structured_output` rather than the explanation. */
function ResolutionDetail({ step }: { step: AgentStep }) {
  const out = step.structured_output as {
    action_type?: string;
    tier?: number;
    shadow?: boolean;
    status?: string;
    expected_effect?: string;
    alternatives_rejected?: string[];
    clamped?: string[];
    compensating_action?: { note?: string };
  } | null;
  if (!out?.action_type) return null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <code className="rounded border border-edge bg-bg px-1.5 py-0.5">{out.action_type}</code>
        <span className="text-muted">tier {out.tier}</span>
        {out.shadow && (
          <span className="rounded-full border border-edge px-2 py-0.5 text-[11px] text-muted">
            shadow mode — will not execute
          </span>
        )}
        {out.status && <span className="text-muted">· {out.status}</span>}
      </div>

      {out.expected_effect && (
        <Section label="Expected effect">
          <p className="text-[13px] leading-relaxed">{out.expected_effect}</p>
        </Section>
      )}

      {out.alternatives_rejected && out.alternatives_rejected.length > 0 && (
        <Section label="Actions rejected">
          <BulletList items={out.alternatives_rejected} />
        </Section>
      )}

      {out.compensating_action?.note && (
        <Section label="If we undo">
          <p className="text-[13px] leading-relaxed">{out.compensating_action.note}</p>
        </Section>
      )}

      {out.clamped && out.clamped.length > 0 && (
        // A clamp means the model asked for something outside its declared bounds and Python
        // refused. That is a safety event, so it renders as one rather than as a footnote.
        <Section label="Clamped by safety bounds">
          <div className="rounded-xl border border-warn/40 bg-warn/5 p-3">
            <BulletList items={out.clamped} />
          </div>
        </Section>
      )}
    </div>
  );
}

/** Observer-specific verdicts, which are the reason to trust or distrust everything above. */
function ObserverDetail({ step }: { step: AgentStep }) {
  const out = step.structured_output as {
    approved?: boolean;
    notes?: string;
    rejected_count?: number;
    category_supported?: boolean;
    category_reason?: string;
    claim_verdicts?: { claim: string; valid: boolean; reason: string; evidence_id: string }[];
  } | null;
  if (!out || out.approved === undefined) return null;

  return (
    <div className="space-y-4">
      <div
        className={`rounded-xl border p-3.5 ${
          out.approved ? "border-ok/40 bg-ok/5" : "border-danger/40 bg-danger/5"
        }`}
      >
        <p className={`text-[13px] font-medium ${out.approved ? "text-ok" : "text-danger"}`}>
          {out.approved
            ? "Approved — every claim resolved to cited evidence"
            : "Rejected — the hypothesis was not published"}
        </p>
        {out.notes && <p className="mt-1 text-[12.5px] text-muted">{out.notes}</p>}
        {out.category_reason && (
          <p className="mt-1 text-[12.5px] text-muted">{out.category_reason}</p>
        )}
      </div>

      {out.claim_verdicts && out.claim_verdicts.length > 0 && (
        <Section label={`Claim verdicts (${out.claim_verdicts.length})`}>
          <ul className="space-y-2">
            {out.claim_verdicts.map((verdict, i) => (
              <li key={i} className="rounded-xl border border-edge bg-bg p-3">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-[10.5px]">
                  <span className={verdict.valid ? "text-ok" : "text-danger"}>
                    {verdict.valid ? "✓ valid" : "✕ invalid"}
                  </span>
                  <code className="text-muted">{verdict.evidence_id}</code>
                  <span className="text-muted">· {verdict.reason}</span>
                </div>
                <p className="text-[13px] leading-relaxed">{verdict.claim}</p>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  );
}

function StepMetrics({ step }: { step: AgentStep }) {
  const bits: string[] = [];
  if (step.model_used) bits.push(step.model_used);
  const latency = formatLatency(step.latency_ms);
  if (latency) bits.push(latency);
  if (step.tokens_used !== null) bits.push(`${groupDigits(step.tokens_used)} tokens`);
  const cost = formatCost(step.cost_usd);
  if (cost) bits.push(cost);
  if (bits.length === 0) return null;
  return (
    <p className="mt-4 border-t border-edge pt-3 font-mono text-[10.5px] text-muted">
      {bits.join(" · ")}
    </p>
  );
}

function AgentCard({
  step,
  passes,
  defaultOpen,
}: {
  step: AgentStep;
  passes: AgentStep[];
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const explanation: AgentExplanation | null = step.explanation;
  const confidence = explanation?.confidence ?? step.confidence;
  const headline = explanation?.headline || step.output_summary || "No explanation recorded";
  const bodyId = `step-body-${step.id}`;

  return (
    <li className="overflow-hidden rounded-2xl border border-edge bg-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={bodyId}
        className="w-full px-5 py-4 text-left transition-colors hover:bg-surface2"
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">
            {step.agent_name}
          </span>
          {passes.length > 0 && (
            <span className="rounded-full border border-edge px-2 py-0.5 text-[10px] text-muted">
              {passes.length}-pass ensemble
            </span>
          )}
          {confidence !== null && confidence !== undefined && (
            <ConfidenceMeter value={confidence} />
          )}
          <span className="ml-auto flex items-center gap-2 text-[11px] text-muted">
            <LocalTime iso={step.created_at} />
            <span aria-hidden="true" className={open ? "rotate-180" : ""}>
              ⌄
            </span>
          </span>
        </div>

        <p className="mt-2 text-[15px] leading-snug tracking-tight">{headline}</p>

        {/* Never behind the expander — see the module note. */}
        {explanation?.uncertainty && (
          <div className="mt-3 border-l-2 border-warn/50 pl-3">
            <span className="text-[10px] uppercase tracking-[0.16em] text-warn">
              Not established
            </span>
            <p className="mt-0.5 text-[12.5px] leading-relaxed text-muted">
              {explanation.uncertainty}
            </p>
          </div>
        )}

        {explanation && explanation.alternatives_considered.length > 0 && (
          <div className="mt-3">
            <span className="text-[10px] uppercase tracking-[0.16em] text-muted">
              Alternatives considered ({explanation.alternatives_considered.length})
            </span>
            <div className="mt-1">
              <BulletList items={explanation.alternatives_considered} />
            </div>
          </div>
        )}
      </button>

      {open && (
        <div id={bodyId} className="space-y-5 border-t border-edge px-5 py-5">
          {!explanation && (
            <p className="text-[13px] text-muted">
              This step recorded no explanation. Explanation is written after the agent&rsquo;s
              real work and never blocks it, so an investigation can succeed without one.
            </p>
          )}

          {explanation?.what_it_received && (
            <Section label="What it received">
              <p className="text-[13px] leading-relaxed text-muted">
                {explanation.what_it_received}
              </p>
            </Section>
          )}

          {explanation?.reasoning && (
            <Section label="Reasoning">
              <p className="text-[13.5px] leading-relaxed">{explanation.reasoning}</p>
            </Section>
          )}

          {explanation && explanation.evidence_collected.length > 0 && (
            <Section label="Evidence collected">
              <BulletList items={explanation.evidence_collected} />
            </Section>
          )}

          {explanation && explanation.tools_used.length > 0 && (
            <Section label="Tools used">
              <div className="flex flex-wrap gap-1.5">
                {explanation.tools_used.map((tool) => (
                  <code
                    key={tool}
                    className="rounded border border-edge bg-bg px-1.5 py-0.5 text-[11.5px]"
                  >
                    {tool}
                  </code>
                ))}
              </div>
            </Section>
          )}

          {explanation && explanation.documents_retrieved.length > 0 && (
            <Section label="Runbooks retrieved">
              <BulletList items={explanation.documents_retrieved} />
            </Section>
          )}

          {step.agent_name === "observer" && <ObserverDetail step={step} />}
          {step.agent_name === "resolution" && <ResolutionDetail step={step} />}

          {step.citations.length > 0 && (
            <Section label={`Citations (${step.citations.length})`}>
              <CitationList citations={step.citations} />
            </Section>
          )}

          {explanation && explanation.recommended_next.length > 0 && (
            <Section label="Recommended next">
              <BulletList items={explanation.recommended_next} />
            </Section>
          )}

          {passes.length > 0 && (
            <Section label={`Ensemble passes (${passes.length})`}>
              <ul className="space-y-1.5">
                {passes.map((pass) => (
                  <li key={pass.id} className="text-[12.5px] text-muted">
                    <span className="font-mono">#{(pass.ensemble_pass_index ?? 0) + 1}</span>{" "}
                    {pass.output_summary ?? "no summary"}
                    {pass.confidence !== null && ` · confidence ${pass.confidence.toFixed(2)}`}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <StepMetrics step={step} />
        </div>
      )}
    </li>
  );
}

/** Supervisor routing decisions render as connectors, not cards — they are control flow. */
function RoutingMarker({ step }: { step: AgentStep }) {
  const out = step.structured_output as {
    next_step?: string;
    available?: string[];
    llm_decided?: boolean;
    overridden_from?: string | null;
  } | null;
  if (!out?.next_step) return null;
  return (
    <li className="flex flex-wrap items-center gap-2 px-5 py-1 text-[11px] text-muted">
      <span aria-hidden="true">↓</span>
      <span>
        routed to <span className="text-fg">{out.next_step}</span>
      </span>
      {out.llm_decided ? (
        <span className="rounded-full border border-edge px-2 py-0.5">
          supervisor chose from {out.available?.length ?? 0}
        </span>
      ) : (
        <span className="rounded-full border border-edge px-2 py-0.5">only legal step</span>
      )}
      {out.overridden_from && (
        <span className="text-warn">overridden from {out.overridden_from}</span>
      )}
    </li>
  );
}

export function AgentTimeline({ steps }: { steps: AgentStep[] }) {
  const [allOpen, setAllOpen] = useState<boolean | null>(null);

  // Ensemble passes belong to their synthesized parent, not to the top level.
  const passesByAgent = new Map<string, AgentStep[]>();
  for (const step of steps) {
    if (step.ensemble_pass_index !== null) {
      const list = passesByAgent.get(step.agent_name) ?? [];
      list.push(step);
      passesByAgent.set(step.agent_name, list);
    }
  }

  const timeline = steps.filter((s) => s.ensemble_pass_index === null);

  if (timeline.length === 0) {
    return (
      <div className="rounded-2xl border border-edge bg-surface px-6 py-14 text-center">
        <p className="text-[15px]">No agent activity yet</p>
        <p className="mt-1.5 text-[13px] text-muted">
          The worker picks up new incidents within seconds. This view updates itself — there is
          nothing to refresh.
        </p>
      </div>
    );
  }

  const explained = timeline.filter((s) => s.agent_name !== "orchestrator");

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-[10px] uppercase tracking-[0.16em] text-muted">
          Agent reasoning ({explained.length})
        </h2>
        <button
          type="button"
          onClick={() => setAllOpen((v) => (v === true ? false : true))}
          className="rounded-full border border-edge px-3 py-1 text-[11px] text-muted transition-colors hover:bg-surface2 hover:text-fg"
        >
          {allOpen ? "Collapse all" : "Expand all"}
        </button>
      </div>

      <ol className="space-y-2">
        {timeline.map((step) => {
          if (step.agent_name === "orchestrator") {
            return <RoutingMarker key={step.id} step={step} />;
          }
          return (
            <AgentCard
              // Remounts every card when the expand-all mode flips, which is what makes a
              // single toggle override each card's own open state.
              key={`${step.id}-${allOpen}`}
              step={step}
              passes={passesByAgent.get(step.agent_name) ?? []}
              defaultOpen={allOpen ?? EXPANDED_BY_DEFAULT.has(step.agent_name)}
            />
          );
        })}
      </ol>
    </div>
  );
}
