// One context-variant: its context (cell lines / label sources / verbatim scope), metrics with
// CI, the limitations block, and the read-only model card. Rendered for each entry in the
// endpoint's `variants` list.

import type { ContextVariant } from "../api/types";
import { LimitationsPanel } from "./LimitationsPanel";
import { MetricsTable } from "./MetricsTable";

export function VariantView({ variant }: { variant: ContextVariant }) {
  const c = variant.context;
  return (
    <div className="space-y-5">
      <section className="rounded-lg border border-line bg-white p-4">
        <h3 className="text-sm font-semibold text-ink mb-2">Biological context</h3>
        <dl className="grid grid-cols-1 sm:grid-cols-3 gap-y-2 gap-x-4 text-sm">
          <dt className="text-muted">Cell lines</dt>
          <dd className="sm:col-span-2 text-ink">
            {c.cell_lines && c.cell_lines.length > 0 ? c.cell_lines.join(", ") : "not specified"}
          </dd>
          <dt className="text-muted">Label sources</dt>
          <dd className="sm:col-span-2 text-ink">
            {c.label_sources.length > 0 ? c.label_sources.join(", ") : "—"}
          </dd>
          <dt className="text-muted">Scope</dt>
          <dd className="sm:col-span-2 text-ink">{c.scope ?? "—"}</dd>
        </dl>
      </section>

      <section>
        <h3 className="text-sm font-semibold text-ink mb-2">Honest performance estimate</h3>
        <MetricsTable metrics={variant.metrics_summary} />
      </section>

      <LimitationsPanel limitations={variant.limitations} />

      <section>
        <h3 className="text-sm font-semibold text-ink mb-2">Model card</h3>
        <pre className="rounded-lg border border-line bg-white p-4 text-xs text-ink whitespace-pre-wrap font-mono overflow-x-auto">
          {variant.model_card_markdown}
        </pre>
      </section>
    </div>
  );
}
