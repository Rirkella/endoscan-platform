// One endpoint's result card (rendered per API result — never hardcoded). Shows the endpoint
// signal score, the model call (Active/Inactive), the threshold, the Experimental status badge,
// a context/variant label, the integrated limitations, and an on-demand Explain button.
// Wording is "Endpoint signal score" / "Model call" — never "risk score / toxic / safe".

import { useState } from "react";

import { api } from "../api/client";
import type { ExplanationResult, Signature } from "../api/types";
import type { EndpointSignal } from "../hooks/useAnalyze";
import { ErrorNotice } from "./ErrorNotice";
import { GeneContributionCards } from "./GeneContributionCards";
import { LimitationsPanel } from "./LimitationsPanel";
import { PathwaysPanel } from "./PathwaysPanel";
import { StatusBadge } from "./StatusBadge";

interface ExplainState {
  loading: boolean;
  result: ExplanationResult | null;
  error: unknown;
  opened: boolean;
}

export function EndpointSignalCard({
  signal,
  signature,
}: {
  signal: EndpointSignal;
  signature: Signature;
}) {
  const [ex, setEx] = useState<ExplainState>({
    loading: false,
    result: null,
    error: null,
    opened: false,
  });

  async function explain() {
    setEx({ loading: true, result: null, error: null, opened: true });
    try {
      // Explain-availability is driven by the LIVE API call (a clean 501/503 renders honestly);
      // any seeded hint must not gate or replace this real call.
      const result = await api.explain(signal.endpoint_id, signature);
      setEx({ loading: false, result, error: null, opened: true });
    } catch (error) {
      setEx({ loading: false, result: null, error, opened: true });
    }
  }

  // Per-card failure — does not affect sibling cards.
  if (signal.error != null || !signal.result) {
    return (
      <article className="rounded-lg border border-line bg-white p-4">
        <header className="mb-2">
          <h3 className="font-semibold text-ink">{signal.biological_target}</h3>
          <p className="font-mono text-xs text-muted">{signal.endpoint_id}</p>
        </header>
        <ErrorNotice error={signal.error} />
      </article>
    );
  }

  const r = signal.result;
  const status = r.limitations.status;

  return (
    <article className="flex flex-col rounded-lg border border-line bg-white p-4">
      <header className="mb-3 flex items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold text-ink">{signal.biological_target}</h3>
          <p className="font-mono text-xs text-muted">{signal.endpoint_id}</p>
        </div>
        <StatusBadge status={status} />
      </header>

      <dl className="mb-3 grid grid-cols-3 gap-2 text-center">
        <div className="rounded-md bg-surface px-2 py-2">
          <dt className="text-[11px] uppercase tracking-wide text-muted">Endpoint signal score</dt>
          <dd className="mt-0.5 text-lg font-semibold tabular-nums text-ink">
            {r.probability.toFixed(2)}
          </dd>
        </div>
        <div className="rounded-md bg-surface px-2 py-2">
          <dt className="text-[11px] uppercase tracking-wide text-muted">Model call</dt>
          <dd
            className={`mt-0.5 text-sm font-semibold ${r.call ? "text-brand" : "text-slate-500"}`}
          >
            {r.call ? "Active" : "Inactive"}
          </dd>
        </div>
        <div className="rounded-md bg-surface px-2 py-2">
          <dt className="text-[11px] uppercase tracking-wide text-muted">Threshold</dt>
          <dd className="mt-0.5 text-sm tabular-nums text-ink">{r.threshold.toFixed(2)}</dd>
        </div>
      </dl>

      <LimitationsPanel limitations={r.limitations} />

      <div className="mt-3">
        {!ex.opened ? (
          <button
            type="button"
            className="rounded-md border border-line bg-white px-3 py-1.5 text-sm font-medium text-ink hover:border-brand"
            onClick={explain}
          >
            Explain this call
          </button>
        ) : ex.loading ? (
          <p className="text-sm text-muted">Explaining…</p>
        ) : ex.error != null ? (
          <ErrorNotice error={ex.error} />
        ) : ex.result ? (
          <>
            <GeneContributionCards explanation={ex.result} />
            {/* Biological pathways for THIS explain result (its own honest empty states). */}
            <PathwaysPanel endpointId={signal.endpoint_id} signature={signature} />
          </>
        ) : null}
      </div>
    </article>
  );
}
