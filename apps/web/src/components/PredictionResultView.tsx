// Prediction result — honesty-first wording (no overclaim). Header describes an endpoint-
// CONSISTENT transcriptomic signal under this model's context; the score is a model score, not
// a real-world toxicity probability; the full limitations block rides on every result.

import type { PredictionResult } from "../api/types";
import { LimitationsBlock } from "./LimitationsBlock";

export function PredictionResultView({
  result,
  target,
}: {
  result: PredictionResult;
  target: string;
}) {
  const r = result;
  const header = r.call
    ? `Transcriptomic signal consistent with ${target} activity`
    : "No endpoint-consistent signal detected";

  return (
    <div className="space-y-4">
      <section className="rounded-lg border border-line bg-white p-4">
        <h3 className="text-base font-semibold text-ink">{header}</h3>
        <p className="text-xs text-muted">under this experimental model&rsquo;s context</p>

        <dl className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-y-1 gap-x-6 text-sm">
          <div className="flex justify-between gap-3">
            <dt className="text-muted">Probability score</dt>
            <dd className="tabular-nums text-ink">{r.probability.toFixed(2)}</dd>
          </div>
          <div className="flex justify-between gap-3">
            <dt className="text-muted">Call (threshold {r.threshold.toFixed(2)})</dt>
            <dd className="text-ink">{r.call ? "positive" : "negative"}</dd>
          </div>
        </dl>
        <p className="mt-2 text-xs text-muted">
          A model score, not a probability of real-world toxicity. Model status is experimental;
          this is not a &ldquo;toxic&rdquo;/&ldquo;safe&rdquo; determination.
        </p>
      </section>

      <LimitationsBlock limitations={r.limitations} />

      <p className="text-xs text-muted">
        This is a research pre-screening result under an experimental model. Not a regulatory,
        clinical, or diagnostic determination.
      </p>
    </div>
  );
}
