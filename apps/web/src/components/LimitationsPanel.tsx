// Integrated limitations — prominent but scannable (not a wall). Compact status header +
// one-line summary + prevalence chip, then the CI-demotion reasons, the claim scope, and a
// persistent disclaimer footer — ALL visible by default. A disclosure adds DEPTH
// (floors_provenance / long-form) that stays in the DOM; nothing honest is truncated away.
// (The long model-card prose lives in the Model Library, not here.)

import type { LimitationsBlock } from "../api/types";
import { StatusBadge } from "./StatusBadge";

function summaryLine(l: LimitationsBlock): string {
  if (l.is_experimental && l.missed_criteria.length > 0) {
    const n = l.missed_criteria.length;
    return `Experimental — ${n} validated-MVP ${n === 1 ? "criterion" : "criteria"} unmet (see below).`;
  }
  if (l.is_experimental) return "Experimental — not a validated predictor.";
  return `Status: ${l.status}.`;
}

function pct(x: number | null): string {
  return x == null ? "—" : `${(x * 100).toFixed(1)}%`;
}

export function LimitationsPanel({ limitations }: { limitations: LimitationsBlock }) {
  const l = limitations;
  return (
    <section className="rounded-lg border border-line bg-white" aria-label="Limitations">
      {/* Compact status header */}
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2.5">
        <StatusBadge status={l.status} />
        <span className="text-sm text-ink">{summaryLine(l)}</span>
        {l.prevalence != null && (
          <span className="ml-auto rounded-full bg-surface px-2 py-0.5 text-xs text-muted">
            prevalence {pct(l.prevalence)}
            {l.positives != null && l.n_total != null && ` (${l.positives}/${l.n_total})`}
          </span>
        )}
      </div>

      <div className="space-y-3 px-4 py-3">
        {/* CI-demotion reasons — scannable list, visible by default */}
        {l.missed_criteria.length > 0 && (
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted">
              Why {l.status}
            </p>
            <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-ink">
              {l.missed_criteria.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          </div>
        )}

        {/* Claim scope — labeled line, visible by default */}
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-muted">Scope of claim</p>
          <p className="mt-0.5 text-sm text-ink">{l.claim_scope}</p>
        </div>

        {/* Depth-only disclosure — remains in the DOM, nothing hidden/truncated */}
        {l.floors_provenance && (
          <details className="text-sm">
            <summary className="cursor-pointer text-muted">Full limitations &amp; provenance</summary>
            <p className="mt-1 text-muted">{l.floors_provenance}</p>
          </details>
        )}
      </div>

      {/* Persistent disclaimer footer */}
      <p className="border-t border-line px-4 py-2 text-xs text-muted">{l.disclaimer}</p>
    </section>
  );
}
