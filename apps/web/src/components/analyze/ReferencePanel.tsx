// Reference context tab (ported from prototype-v2's reference landscape) wired to the REAL API.
// It loads the committed data-space map for a chosen endpoint (GET /explore/{ctx}/umap), places
// the current signature APPROXIMATELY (POST /explore/locate — nearest neighbours in the full
// feature space, never an exact projection), and highlights the similar compounds. Honesty holds:
// distance is descriptive context, not a prediction or proof of shared mechanism; there is no
// in-/out-of-domain verdict here. The full technical breakdown lives on the dedicated Reference
// data screen (/explore), linked below, so this embed stays plain and biological.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { EndoscanApiError, api } from "../../api/client";
import type { EndpointSummary, Signature } from "../../api/types";
import { useAsync } from "../../hooks/useAsync";
import { useExplore } from "../../hooks/useExplore";
import { ErrorNotice } from "../ErrorNotice";
import { ExploreScatter } from "../ExploreScatter";

// Plain similarity band from the real percentile (higher percentile = more isolated / further).
function similarityBand(percentile: number): "close" | "moderately close" | "far" {
  if (percentile <= 0.5) return "close";
  if (percentile <= 0.9) return "moderately close";
  return "far";
}

export function ReferencePanel({
  endpoints,
  signature,
}: {
  endpoints: EndpointSummary[];
  signature: Signature;
}) {
  const [context, setContext] = useState(endpoints[0]?.endpoint_id ?? "");
  const map = useAsync(
    () => (context ? api.exploreUmap(context) : Promise.reject(new Error("no context"))),
    [context],
  );
  const explore = useExplore(context);

  // Place the current signature whenever the endpoint context (or its map) is ready.
  useEffect(() => {
    if (context && map.data) explore.locate(signature);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context, map.data]);

  const notComputed = map.error instanceof EndoscanApiError && map.error.status === 404;

  return (
    <div className="reference-context-layout">
      <section className="reference-map-card">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Reference landscape</p>
            <h2>Where this signature sits</h2>
            <p>Distance is descriptive context, not a model prediction.</p>
          </div>
          {endpoints.length > 1 && (
            <label className="reference-context-select">
              Endpoint
              <select
                value={context}
                onChange={(e) => {
                  setContext(e.target.value);
                  explore.reset();
                }}
              >
                {endpoints.map((e) => (
                  <option key={e.endpoint_id} value={e.endpoint_id}>
                    {e.endpoint_id}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        {(map.loading || !context) && <p className="check-plain">Loading the reference landscape…</p>}

        {notComputed && (
          <div className="no-signature">
            <strong>No reference map for {context} yet</strong>
            <p>
              This stays empty until a real map is computed on the server — no placeholder points
              are shown.
            </p>
          </div>
        )}

        {map.error != null && !notComputed && <ErrorNotice error={map.error} />}

        {map.data && (
          <>
            <ExploreScatter points={map.data.points} locate={explore.result} />
            <details className="mt-3 text-xs text-muted">
              <summary className="cursor-pointer font-medium text-ink">
                Reference data provenance
              </summary>
              <p className="mt-1">{map.data.manifest.point_definition}</p>
              <p className="mt-1 break-words">
                Source {map.data.manifest.source_key} · SHA-256 {map.data.manifest.source_sha256}
              </p>
            </details>
            <div className="result-reference-note">
              <span className="status-dot status-dot-blue" aria-hidden />
              This map shows how known reference signatures relate to each other. It is a visual
              guide — not a model boundary, and not proof of anything.
            </div>
          </>
        )}
      </section>

      <aside className="neighbor-panel">
        <p className="aside-label">Placement</p>
        <h2>Similar known signatures</h2>

        {explore.running && <p className="check-plain">Finding similar compounds…</p>}
        {explore.error != null && <ErrorNotice error={explore.error} />}

        {explore.result ? (
          <>
            <p className="check-plain" data-testid="reference-similarity">
              Approximate position based on the most similar known signatures. This signature is{" "}
              <strong>{similarityBand(explore.result.domain.percentile)}</strong> to EndoScan&rsquo;s
              reference data — a graded comparison, not a pass/fail verdict.
            </p>
            <p className="aside-label" style={{ marginTop: "16px" }}>
              Compounds with similar expression patterns
            </p>
            <ul className="reference-neighbors">
              {explore.result.neighbors.map((n) => (
                <li key={n.compound_id}>
                  <span className="mono">{n.compound_id}</span>
                  <span>Distance {n.distance.toFixed(3)}</span>
                  {n.label && <span>Endpoint label: {n.label}</span>}
                </li>
              ))}
            </ul>
            {explore.result.neighbors.length === 0 && (
              <p className="check-plain">No reference neighbours were returned for this signature.</p>
            )}
            <p className="check-plain">Similar patterns do not prove the same effect.</p>
          </>
        ) : (
          !explore.running &&
          map.data && <p className="check-plain">Placing this signature in the reference space…</p>
        )}

        <div style={{ marginTop: "16px" }}>
          <Link className="detail-link" to="/explore">
            Open the full reference view
          </Link>
        </div>
      </aside>
    </div>
  );
}
