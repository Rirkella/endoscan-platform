// Evidence tab (ported layout from prototype-v2) wired to the REAL API. An endpoint selector on the
// left (built from the endpoints that actually scored — never hardcoded); on the right, for the
// selected endpoint, a live POST /explain drives the reused honesty components:
//   - GeneContributionCards  : real method label per endpoint (TreeSHAP vs linear-coefficient),
//                              never mislabeled;
//   - PathwaysPanel          : real Reactome over-representation with its honest empty states;
//   - LimitationsPanel       : the endpoint's real limitations + experimental status.
// A 501/503 from /explain renders as a calm "not available" state, not a failure.

import { useState } from "react";

import { api } from "../../api/client";
import { predictionScore, type PathwaysResponse, type Signature } from "../../api/types";
import type { EndpointSignal } from "../../hooks/useAnalyze";
import { useAsync } from "../../hooks/useAsync";
import { ErrorNotice } from "../ErrorNotice";
import { GeneContributionCards } from "../GeneContributionCards";
import { LimitationsPanel } from "../LimitationsPanel";
import { LiteraturePanel } from "../LiteraturePanel";
import { PathwaysPanel } from "../PathwaysPanel";

function endpointCodeClass(id: string): string {
  const k = id.toLowerCase();
  return k === "er" ? "code-er" : k === "ar" ? "code-ar" : "code-generic";
}

export function EvidencePanel({
  signals,
  signature,
}: {
  signals: EndpointSignal[];
  signature: Signature;
}) {
  // Only endpoints that produced a result can be explained.
  const scored = signals.filter((s) => s.result != null);
  const [selected, setSelected] = useState(scored[0]?.endpoint_id ?? "");

  if (scored.length === 0) {
    return (
      <div className="evidence-empty">
        <p>No endpoint produced a result to explain for this signature.</p>
      </div>
    );
  }

  const active = scored.find((s) => s.endpoint_id === selected) ?? scored[0];

  return (
    <div className="evidence-layout">
      <aside className="endpoint-selector">
        <p className="aside-label">Endpoints</p>
        {scored.map((s) => {
          const r = s.result!;
          return (
            <button
              key={s.endpoint_id}
              className={s.endpoint_id === active.endpoint_id ? "selected" : ""}
              onClick={() => setSelected(s.endpoint_id)}
            >
              <span className={`endpoint-code ${endpointCodeClass(s.endpoint_id)}`}>
                {s.endpoint_id}
              </span>
              <span>
                <strong>{s.biological_target}</strong>
                <small>
                  Score {predictionScore(r).toFixed(2)} / {r.call ? "above threshold" : "below threshold"}
                </small>
              </span>
            </button>
          );
        })}
      </aside>

      <div className="evidence-main">
        <div className="evidence-title">
          <div>
            <p className="eyebrow">Why this result</p>
            <h2>{active.biological_target} evidence</h2>
          </div>
          <span className={`status-chip ${active.result!.call ? "status-signal" : "status-neutral"}`}>
            {active.result!.call ? "Active call" : "Inactive call"}
          </span>
        </div>

        <EndpointExplanation
          key={active.endpoint_id}
          endpointId={active.endpoint_id}
          signature={signature}
        />

        <section className="evidence-embed">
          <LimitationsPanel limitations={active.result!.limitations} />
        </section>
      </div>
    </div>
  );
}

function EndpointExplanation({
  endpointId,
  signature,
}: {
  endpointId: string;
  signature: Signature;
}) {
  // Live explain call — availability is driven by the real API (a clean 501/503 renders honestly).
  const state = useAsync(() => api.explain(endpointId, signature), [endpointId]);
  const [pathways, setPathways] = useState<PathwaysResponse | null>(null);

  return (
    <>
      <section className="evidence-embed">
        {state.loading && <p className="embed-copy">Preparing gene contributions…</p>}
        {state.error != null && <ErrorNotice error={state.error} />}
        {state.data && <GeneContributionCards explanation={state.data} />}
      </section>
      {/* Pathways run off THIS endpoint's explain result (own honest empty/too-few/unavailable states). */}
      <section className="evidence-embed">
        <PathwaysPanel endpointId={endpointId} signature={signature} onResult={setPathways} />
      </section>
      {state.data && pathways && (
        <section className="evidence-embed">
          <LiteraturePanel
            endpointId={endpointId}
            genes={state.data.top_contributors.slice(0, 8).map((item) => item.gene)}
            pathways={pathways.pathways.slice(0, 5).map((item) => ({
              pathway_id: item.pathway_id,
              name: item.name,
              genes: item.genes_influencing_result,
            }))}
          />
        </section>
      )}
    </>
  );
}
