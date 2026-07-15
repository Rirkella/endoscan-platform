// Count-adaptive comparison of endpoint signal scores. The contract takes the results array and
// selects the visualization by LENGTH, so adding views later is additive:
//   - 2 endpoints  -> ComparisonBars (BUILT)
//   - >= 3         -> honest "coming later" panel (radar/ranked views need real multi-endpoint
//                     data; NO fake radar/heatmap is rendered in Phase 1)
//   - < 2          -> nothing (a single result needs no comparison)
// Only successful predictions participate.

import { predictionScore } from "../api/types";
import type { EndpointSignal } from "../hooks/useAnalyze";

interface Scored {
  endpoint_id: string;
  biological_target: string;
  score: number;
  call: boolean;
  threshold: number;
}

function scored(signals: EndpointSignal[]): Scored[] {
  return signals
    .filter((s) => s.result != null)
    .map((s) => ({
      endpoint_id: s.endpoint_id,
      biological_target: s.biological_target,
      score: predictionScore(s.result!),
      call: s.result!.call,
      threshold: s.result!.threshold,
    }));
}

function ComparisonBars({ items }: { items: Scored[] }) {
  return (
    <div className="rounded-lg border border-line bg-white p-4" data-viz="bars">
      <h3 className="mb-3 text-sm font-semibold text-ink">Endpoint signal comparison</h3>
      <ul className="space-y-3">
        {items.map((it) => (
          <li key={it.endpoint_id}>
            <div className="mb-1 flex items-center justify-between text-sm">
              <span className="text-ink">
                {it.biological_target}{" "}
                <span className="font-mono text-xs text-muted">({it.endpoint_id})</span>
              </span>
              <span className="tabular-nums text-muted">
                {it.score.toFixed(2)} · {it.call ? "Above threshold" : "Below threshold"}
              </span>
            </div>
            <div className="relative h-3 w-full overflow-hidden rounded bg-surface">
              <div
                className={`h-full ${it.call ? "bg-brand" : "bg-slate-300"}`}
                style={{ width: `${Math.round(it.score * 100)}%` }}
              />
              {/* threshold marker */}
              <div
                className="absolute top-0 h-full w-px bg-ink/40"
                style={{ left: `${Math.round(it.threshold * 100)}%` }}
                title={`threshold ${it.threshold.toFixed(2)}`}
              />
            </div>
          </li>
        ))}
      </ul>
      <p className="mt-3 text-xs text-muted">
        Bars show each endpoint&rsquo;s signal score; the tick marks the model&rsquo;s call threshold.
      </p>
    </div>
  );
}

function VizComingLater({ count }: { count: number }) {
  return (
    <div className="rounded-lg border border-dashed border-line bg-surface p-4" data-viz="coming-later">
      <h3 className="text-sm font-semibold text-ink">Multi-endpoint comparison</h3>
      <p className="mt-1 text-sm text-muted">
        A radar / ranked view for {count} endpoints is coming in a later phase. Per-endpoint results
        are shown as cards above.
      </p>
    </div>
  );
}

export function ComparisonViz({ signals }: { signals: EndpointSignal[] }) {
  const items = scored(signals);
  if (items.length < 2) return null;
  if (items.length === 2) return <ComparisonBars items={items} />;
  return <VizComingLater count={items.length} />;
}
