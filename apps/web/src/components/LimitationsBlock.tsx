// The honest limitations block — rendered verbatim from the API's LimitationsBlock. Front-and-
// center on detail and repeated on every prediction/explanation result. Nothing is hardcoded.

import type { LimitationsBlock as Limitations } from "../api/types";
import { StatusBadge } from "./StatusBadge";

function pct(x: number | null): string {
  return x == null ? "—" : `${(x * 100).toFixed(1)}%`;
}

export function LimitationsBlock({ limitations }: { limitations: Limitations }) {
  const l = limitations;
  return (
    <section className="rounded-lg border border-line bg-white p-4" aria-label="Limitations">
      <div className="flex items-center gap-2 mb-3">
        <h3 className="text-sm font-semibold text-ink">Limitations &amp; honest scope</h3>
        <StatusBadge status={l.status} />
      </div>

      {l.missed_criteria.length > 0 && (
        <div className="mb-3">
          <p className="text-xs font-medium text-muted mb-1">
            Why this model is {l.status} — unmet validated criteria:
          </p>
          <ul className="list-disc pl-5 space-y-1 text-sm text-ink">
            {l.missed_criteria.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1 text-sm">
        <div className="flex justify-between gap-3">
          <dt className="text-muted">Scope of claim</dt>
        </div>
        <div />
        <p className="sm:col-span-2 text-ink">{l.claim_scope}</p>
        {l.prevalence != null && (
          <div className="flex justify-between gap-3">
            <dt className="text-muted">Positive prevalence</dt>
            <dd className="text-ink">
              {pct(l.prevalence)}
              {l.positives != null && l.n_total != null && (
                <span className="text-muted">
                  {" "}
                  ({l.positives}/{l.n_total})
                </span>
              )}
            </dd>
          </div>
        )}
        {l.floors_provenance && (
          <div className="sm:col-span-2 text-muted text-xs">{l.floors_provenance}</div>
        )}
      </dl>

      <p className="mt-3 text-xs text-muted border-t border-line pt-2">{l.disclaimer}</p>
    </section>
  );
}
