import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { EndoscanApiError, api } from "../../api/client";
import {
  predictionScore,
  type ExplanationCapabilityStatus,
  type ExplanationResult,
  type LiteratureResponse,
  type PathwaysResponse,
  type Signature,
} from "../../api/types";
import type { EndpointSignal } from "../../hooks/useAnalyze";
import { GeneContributionCards } from "../GeneContributionCards";
import { LiteraturePanel } from "../LiteraturePanel";
import { PathwaysPanel } from "../PathwaysPanel";

export function EvidencePanel({
  signals,
  signature,
  compound,
  selectedEndpoint,
  onSelectedEndpoint,
  explanationCache = {},
  pathwayCache = {},
  literatureCache = {},
  onExplanation,
  onPathways,
  onLiterature,
  onCapabilityError,
}: {
  signals: EndpointSignal[];
  signature: Signature;
  compound?: string;
  selectedEndpoint?: string | null;
  onSelectedEndpoint?: (endpointId: string) => void;
  explanationCache?: Record<string, ExplanationResult>;
  pathwayCache?: Record<string, PathwaysResponse>;
  literatureCache?: Record<string, LiteratureResponse>;
  onExplanation?: (endpointId: string, result: ExplanationResult) => void;
  onPathways?: (endpointId: string, result: PathwaysResponse) => void;
  onLiterature?: (endpointId: string, result: LiteratureResponse) => void;
  onCapabilityError?: (key: string, error: unknown) => void;
}) {
  const scored = signals.filter((signal) => signal.result != null);
  const [internalSelected, setInternalSelected] = useState(scored[0]?.endpoint_id ?? "");
  const selected = selectedEndpoint ?? internalSelected;

  function select(endpointId: string) {
    setInternalSelected(endpointId);
    onSelectedEndpoint?.(endpointId);
  }

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
            onClick={() => select(signal.endpoint_id)}
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
          initialExplanation={explanationCache[active.endpoint_id]}
          initialPathways={pathwayCache[active.endpoint_id]}
          initialLiterature={literatureCache[active.endpoint_id]}
          onExplanation={(result) => onExplanation?.(active.endpoint_id, result)}
          onPathways={(result) => onPathways?.(active.endpoint_id, result)}
          onLiterature={(result) => onLiterature?.(active.endpoint_id, result)}
          onCapabilityError={(capability, error) => onCapabilityError?.(`${capability}:${active.endpoint_id}`, error)}
        />
        <section className="compact-model-status" aria-label="Model status">
          <span className="status-chip status-note">Experimental model</span>
          <p>This model is still undergoing validation and is intended for research prioritisation only.</p>
          <Link to={`/library/${encodeURIComponent(active.endpoint_id)}`}>View model validation</Link>
        </section>
      </div>
    </div>
  );
}

function EndpointExplanation({
  endpointId,
  signature,
  compound,
  capability,
  initialExplanation,
  initialPathways,
  initialLiterature,
  onExplanation,
  onPathways,
  onLiterature,
  onCapabilityError,
}: {
  endpointId: string;
  signature: Signature;
  compound?: string;
  capability: ExplanationCapabilityStatus;
  initialExplanation?: ExplanationResult;
  initialPathways?: PathwaysResponse;
  initialLiterature?: LiteratureResponse;
  onExplanation?: (result: ExplanationResult) => void;
  onPathways?: (result: PathwaysResponse) => void;
  onLiterature?: (result: LiteratureResponse) => void;
  onCapabilityError?: (capability: string, error: unknown) => void;
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
  return <AvailableExplanation
    endpointId={endpointId}
    signature={signature}
    compound={compound}
    initialExplanation={initialExplanation}
    initialPathways={initialPathways}
    initialLiterature={initialLiterature}
    onExplanation={onExplanation}
    onPathways={onPathways}
    onLiterature={onLiterature}
    onCapabilityError={onCapabilityError}
  />;
}

function AvailableExplanation({
  endpointId,
  signature,
  compound,
  initialExplanation,
  initialPathways,
  initialLiterature,
  onExplanation,
  onPathways,
  onLiterature,
  onCapabilityError,
}: {
  endpointId: string;
  signature: Signature;
  compound?: string;
  initialExplanation?: ExplanationResult;
  initialPathways?: PathwaysResponse;
  initialLiterature?: LiteratureResponse;
  onExplanation?: (result: ExplanationResult) => void;
  onPathways?: (result: PathwaysResponse) => void;
  onLiterature?: (result: LiteratureResponse) => void;
  onCapabilityError?: (capability: string, error: unknown) => void;
}) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ data: ExplanationResult | null; error: unknown; loading: boolean }>({
    data: initialExplanation ?? null,
    error: null,
    loading: !initialExplanation,
  });
  const [pathways, setPathways] = useState<PathwaysResponse | null>(initialPathways ?? null);

  useEffect(() => {
    if (initialExplanation && attempt === 0) {
      setState({ data: initialExplanation, error: null, loading: false });
      return;
    }
    let alive = true;
    setState((current) => ({ data: attempt > 0 ? current.data : null, error: null, loading: true }));
    api.explain(endpointId, signature)
      .then((data) => {
        if (!alive) return;
        setState({ data, error: null, loading: false });
        onExplanation?.(data);
      })
      .catch((error) => {
        if (!alive) return;
        setState((current) => ({ data: current.data, error, loading: false }));
        onCapabilityError?.("explanation", error);
      });
    return () => { alive = false; };
    // Persistence callbacks are intentionally not dependencies; changing a parent callback must not
    // repeat an explanation request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt, endpointId, signature]);

  if (state.error != null) {
    const requestId = state.error instanceof EndoscanApiError ? state.error.request_id : null;
    return (
      <>
        <section className="evidence-embed explanation-error" role="alert">
          <strong>Endpoint explanation could not be prepared.</strong>
          <button className="detail-link" type="button" onClick={() => setAttempt((value) => value + 1)}>Retry explanation</button>
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
          <section className="evidence-embed"><PathwaysPanel
            endpointId={endpointId}
            signature={signature}
            initialResult={initialPathways}
            onResult={(result) => { setPathways(result); onPathways?.(result); }}
            onError={(error) => onCapabilityError?.("pathways", error)}
          /></section>
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
              initialResult={initialLiterature}
              onResult={onLiterature}
              onError={(error) => onCapabilityError?.("literature", error)}
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
