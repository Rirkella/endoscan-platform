import { useEffect } from "react";

import { api } from "../api/client";
import type { PathwayCard, PathwaysResponse, Signature } from "../api/types";
import { useAsync } from "../hooks/useAsync";
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
}: {
  endpointId: string;
  signature: Signature;
  onResult?: (result: PathwaysResponse) => void;
}) {
  const state = useAsync(() => api.interpretPathways(endpointId, signature), [endpointId, signature]);
  const supported = (state.data?.pathways ?? []).filter((card) => card.q_value < 0.05);

  useEffect(() => {
    if (state.data) onResult?.(state.data);
  }, [state.data, onResult]);

  return (
    <section className="endpoint-pathways" data-testid="pathways-panel">
      <h3>Endpoint-specific pathway associations</h3>
      {state.loading && <p className="check-plain">Checking pathway associations…</p>}
      {state.error != null && <ErrorNotice error={state.error} />}
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
