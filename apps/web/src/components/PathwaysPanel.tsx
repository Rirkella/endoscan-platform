import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { PathwayCard, PathwaysResponse, Signature } from "../api/types";
import { ErrorNotice } from "./ErrorNotice";

function PathwayCardView({ card }: { card: PathwayCard }) {
  return (
    <li className="endpoint-pathway-card">
      <h5>{card.name}</h5>
      <p>Associated genes: {card.genes_influencing_result.slice(0, 6).join(", ")}</p>
      <span>FDR {card.q_value < 0.001 ? "< 0.001" : card.q_value.toFixed(3)}</span>
    </li>
  );
}

export function PathwaysPanel({
  endpointId,
  signature,
  onResult,
  initialResult,
  onError,
}: {
  endpointId: string;
  signature: Signature;
  onResult?: (result: PathwaysResponse) => void;
  initialResult?: PathwaysResponse | null;
  onError?: (error: unknown) => void;
}) {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{ data: PathwaysResponse | null; error: unknown; loading: boolean }>({
    data: initialResult ?? null,
    error: null,
    loading: !initialResult,
  });

  useEffect(() => {
    if (initialResult && attempt === 0) {
      setState({ data: initialResult, error: null, loading: false });
      return;
    }
    let alive = true;
    setState((current) => ({ data: attempt > 0 ? current.data : null, error: null, loading: true }));
    api.interpretPathways(endpointId, signature)
      .then((data) => {
        if (!alive) return;
        setState({ data, error: null, loading: false });
        onResult?.(data);
      })
      .catch((error) => {
        if (!alive) return;
        setState((current) => ({ data: current.data, error, loading: false }));
        onError?.(error);
      });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempt, endpointId, signature]);
  const supported = (state.data?.pathways ?? []).filter((card) => card.q_value < 0.05);

  return (
    <section className="endpoint-pathways" data-testid="pathways-panel">
      <h3>Endpoint-specific pathway associations</h3>
      {state.loading && <p className="check-plain">Checking pathway associations…</p>}
      {state.error != null && <ErrorNotice error={state.error} />}
      {state.error != null && <button className="detail-link" type="button" onClick={() => setAttempt((value) => value + 1)}>Retry pathway calculation</button>}
      {state.data && (state.data.status !== "ok" || supported.length === 0) && (
        <p className="check-plain">No endpoint-specific pathway association reached the current evidence threshold.</p>
      )}
      {supported.length > 0 && (
        <ul className="endpoint-pathway-list">
          {supported.map((card) => <PathwayCardView key={card.pathway_id} card={card} />)}
        </ul>
      )}
    </section>
  );
}
