// Metrics with the uncertainty interval shown ALONGSIDE each point estimate — never the point
// value alone (honesty: at thin data the CI is what tells the story). All values from the API's
// metrics_summary (point metrics + the grouped-bootstrap `uncertainty` block).

import type { MetricCI, MetricsSummary } from "../api/types";

const METRICS: { key: keyof MetricsSummary; label: string }[] = [
  { key: "auroc", label: "AUROC" },
  { key: "auprc", label: "AUPRC" },
  { key: "balanced_accuracy", label: "Balanced accuracy" },
  { key: "brier_score", label: "Brier score" },
];

function ci(uncertainty: Record<string, unknown> | null, key: string): MetricCI | null {
  const block = uncertainty?.[key];
  if (block && typeof block === "object" && "lo" in block && "hi" in block) {
    return block as MetricCI;
  }
  return null;
}

function fmt(x: number | null | undefined): string {
  return x == null ? "—" : x.toFixed(3);
}

export function MetricsTable({ metrics }: { metrics: MetricsSummary }) {
  return (
    <div className="overflow-hidden rounded-lg border border-line bg-white">
      <table className="w-full text-sm">
        <thead className="bg-surface text-muted">
          <tr>
            <th className="text-left font-medium px-4 py-2">Metric</th>
            <th className="text-right font-medium px-4 py-2">Point</th>
            <th className="text-right font-medium px-4 py-2">95% CI (grouped bootstrap)</th>
          </tr>
        </thead>
        <tbody>
          {METRICS.map(({ key, label }) => {
            const point = metrics[key] as number | null;
            const interval = ci(metrics.uncertainty, key);
            return (
              <tr key={key} className="border-t border-line">
                <td className="px-4 py-2 text-ink">{label}</td>
                <td className="px-4 py-2 text-right tabular-nums text-ink">{fmt(point)}</td>
                <td className="px-4 py-2 text-right tabular-nums text-muted">
                  {interval ? `[${fmt(interval.lo)}, ${fmt(interval.hi)}]` : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p className="px-4 py-2 text-xs text-muted border-t border-line">
        Intervals are a guard against lucky-split point estimates — at small positive counts they
        are wide and themselves imprecise.
      </p>
    </div>
  );
}
