// Top gene contributions from /explain — improved card presentation. The method LABEL is
// rendered from the API's `method` (never assumed); a linear coefficient×value is never
// SHAP-labeled. Each card has an EMPTY annotation slot (Phase 3 fills gene descriptions/links;
// no fabricated text now). Contributions explain THIS model's decision, not biology.

import type { ExplanationResult } from "../api/types";

const METHOD_LABELS: Record<string, string> = {
  tree_shap: "TreeSHAP",
  linear_coefficient: "Linear coefficient attribution (coefficient × value — not a SHAP value)",
};

export function GeneContributionCards({ explanation }: { explanation: ExplanationResult }) {
  const methodLabel = METHOD_LABELS[explanation.method] ?? explanation.method;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-sm font-semibold text-ink">Top gene contributions</h4>
        <span className="text-xs text-muted">
          Method: <span className="font-medium text-ink">{methodLabel}</span>
          {" · "}baseline (logit) {explanation.base_value.toFixed(3)} · {explanation.n_features} genes
        </span>
      </div>

      <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {explanation.top_contributors.map((c) => (
          <li key={c.gene} className="rounded-md border border-line bg-white px-3 py-2">
            <div className="flex items-center justify-between">
              <span className="font-mono text-sm text-ink">{c.gene}</span>
              <span className="flex items-center gap-2 text-sm">
                <span className={c.direction === "toward" ? "text-brand" : "text-slate-500"}>
                  {c.direction === "toward" ? "→ toward" : "← away"}
                </span>
                <span className="tabular-nums text-muted">
                  {c.shap_value >= 0 ? "+" : ""}
                  {c.shap_value.toFixed(4)}
                </span>
              </span>
            </div>
            {/* Annotation slot — intentionally empty until Phase 3 (no fabricated descriptions). */}
            <div data-annotation-slot="" aria-hidden="true" />
          </li>
        ))}
      </ul>

      <p className="text-xs text-muted">
        Gene contributions show what drove THIS model&rsquo;s decision — not biological causality or
        regulatory validation.
      </p>
    </div>
  );
}
