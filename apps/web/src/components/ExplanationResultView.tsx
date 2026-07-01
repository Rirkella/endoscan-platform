// Explanation result — the method LABEL is rendered from the API's `method` field (never
// assumed), and the contribution values are labeled per method so a linear coefficient×value is
// never presented as a SHAP value. Contributions explain THIS model's decision, not biology.

import type { ExplanationResult } from "../api/types";
import { LimitationsBlock } from "./LimitationsBlock";

const METHOD_LABELS: Record<string, string> = {
  tree_shap: "TreeSHAP",
  linear_coefficient: "Linear coefficient attribution (coefficient × value — not a SHAP value)",
};

export function ExplanationResultView({ result }: { result: ExplanationResult }) {
  const r = result;
  const methodLabel = METHOD_LABELS[r.method] ?? r.method;

  return (
    <div className="space-y-4">
      <section className="rounded-lg border border-line bg-white p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-base font-semibold text-ink">Top gene contributions</h3>
          <span className="text-xs text-muted">
            Explanation method: <span className="font-medium text-ink">{methodLabel}</span>
          </span>
        </div>
        <p className="text-xs text-muted">
          Baseline (logit): {r.base_value.toFixed(3)} · {r.n_features} landmark genes
        </p>

        <ul className="mt-3 divide-y divide-line">
          {r.top_contributors.map((c) => (
            <li key={c.gene} className="flex items-center justify-between py-1.5 text-sm">
              <span className="font-mono text-ink">{c.gene}</span>
              <span className="flex items-center gap-3">
                <span
                  className={
                    c.direction === "toward" ? "text-brand" : "text-slate-500"
                  }
                >
                  {c.direction === "toward" ? "→ toward" : "← away"}
                </span>
                <span className="tabular-nums text-muted w-20 text-right">
                  {c.shap_value >= 0 ? "+" : ""}
                  {c.shap_value.toFixed(4)}
                </span>
              </span>
            </li>
          ))}
        </ul>

        <p className="mt-3 text-xs text-muted border-t border-line pt-2">
          Gene contributions show what drove THIS model&rsquo;s decision — not biological
          causality or regulatory validation.
        </p>
      </section>

      <LimitationsBlock limitations={r.limitations} />
    </div>
  );
}
