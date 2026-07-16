import { useState } from "react";

import { EndoscanApiError, api } from "../../api/client";
import { predictionScore, type ExplanationCapabilityStatus, type PathwaysResponse, type Signature } from "../../api/types";
import type { EndpointSignal } from "../../hooks/useAnalyze";
import { useAsync } from "../../hooks/useAsync";
import { GeneContributionCards } from "../GeneContributionCards";
import { LimitationsPanel } from "../LimitationsPanel";
import { LiteraturePanel } from "../LiteraturePanel";
import { PathwaysPanel } from "../PathwaysPanel";

export function EvidencePanel({
  signals,
  signature,
  compound,
}: {
  signals: EndpointSignal[];
  signature: Signature;
  compound?: string;
}) {
  const scored = signals.filter((signal) => signal.result != null);
  const [selected, setSelected] = useState(scored[0]?.endpoint_id ?? "");

  if (scored.length === 0) return <div className="evidence-empty"><p>No endpoint produced a result to explain for this signature.</p></div>;
  const active = scored.find((signal) => signal.endpoint_id === selected) ?? scored[0];

  return (
    <div className="evidence-layout">
      <aside className="endpoint-selector">
        <p className="aside-label">Endpoints</p>
        {scored.map((signal) => (
          <button
            key={signal.endpoint_id}
            className={signal.endpoint_id === active.endpoint_id ? "selected" : ""}
            onClick={() => setSelected(signal.endpoint_id)}
          >
            <span className="endpoint-code code-generic">{signal.endpoint_id}</span>
            <span>
              <strong>{signal.biological_target}</strong>
              <small>Score {predictionScore(signal.result!).toFixed(2)} / {signal.result!.call ? "above threshold" : "below threshold"}</small>
            </span>
          </button>
        ))}
      </aside>

      <div className="evidence-main">
        <div className="evidence-title">
          <div><p className="eyebrow">Endpoint-specific model evidence</p><h2>{active.biological_target} evidence</h2></div>
          <span className={`status-chip ${active.result!.call ? "status-signal" : "status-neutral"}`}>
            {active.result!.call ? "Above threshold" : "Below threshold"}
          </span>
        </div>
        <EndpointExplanation
          key={active.endpoint_id}
          endpointId={active.endpoint_id}
          signature={signature}
          compound={compound}
          capability={active.explanation}
        />
        <section className="evidence-embed"><LimitationsPanel limitations={active.result!.limitations} /></section>
      </div>
    </div>
  );
}

function EndpointExplanation({
  endpointId,
  signature,
  compound,
  capability,
}: {
  endpointId: string;
  signature: Signature;
  compound?: string;
  capability: ExplanationCapabilityStatus;
}) {
  if (!capability.available) {
    return (
      <>
        <section className="evidence-embed explanation-error" role="alert">
          <strong>Endpoint explanation is not available.</strong>
          <p>{capability.reason ?? "This endpoint does not declare a usable explanation capability in the current runtime."}</p>
        </section>
        <DependentEvidenceUnavailable />
      </>
    );
  }
  return <AvailableExplanation endpointId={endpointId} signature={signature} compound={compound} />;
}

function AvailableExplanation({ endpointId, signature, compound }: { endpointId: string; signature: Signature; compound?: string }) {
  const [attempt, setAttempt] = useState(0);
  const state = useAsync(() => api.explain(endpointId, signature), [endpointId, signature, attempt]);
  const [pathways, setPathways] = useState<PathwaysResponse | null>(null);

  if (state.error != null) {
    const requestId = state.error instanceof EndoscanApiError ? state.error.request_id : null;
    return (
      <>
        <section className="evidence-embed explanation-error" role="alert">
          <strong>Endpoint explanation could not be prepared.</strong>
          <button className="detail-link" type="button" onClick={() => setAttempt((value) => value + 1)}>Retry</button>
          {requestId && requestId !== "unavailable" && (
            <details><summary>Error details</summary><p>Request ID: <span className="mono">{requestId}</span></p></details>
          )}
        </section>
        <DependentEvidenceUnavailable />
      </>
    );
  }

  return (
    <>
      <section className="evidence-embed">
        {state.loading && <p className="embed-copy">Preparing gene contributions…</p>}
        {state.data && <GeneContributionCards explanation={state.data} />}
      </section>
      {state.data && (
        <>
          <section className="evidence-embed"><PathwaysPanel endpointId={endpointId} signature={signature} onResult={setPathways} /></section>
          <section className="evidence-embed">
            <LiteraturePanel
              endpointId={endpointId}
              genes={state.data.top_contributors.slice(0, 10).map((item) => item.gene)}
              pathways={(pathways?.pathways ?? []).filter((item) => item.q_value < 0.05).slice(0, 5).map((item) => ({
                pathway_id: item.pathway_id,
                name: item.name,
                genes: item.genes_influencing_result,
              }))}
              compound={compound}
            />
          </section>
        </>
      )}
    </>
  );
}

function DependentEvidenceUnavailable() {
  return <p className="dependent-evidence-note">Pathway and literature context are unavailable because endpoint explanation failed.</p>;
}
