import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { EndoscanApiError, api } from "../../api/client";
import type { EndpointSummary, Signature } from "../../api/types";
import { useAsync } from "../../hooks/useAsync";
import { useExplore } from "../../hooks/useExplore";
import { ErrorNotice } from "../ErrorNotice";
import { ExploreScatter } from "../ExploreScatter";

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

  useEffect(() => {
    if (context && map.data) explore.locate(signature);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [context, map.data, signature]);

  const notComputed = map.error instanceof EndoscanApiError && map.error.status === 404;
  return (
    <div className="reference-context-layout">
      <section className="reference-map-card">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Measured public reference signatures</p>
            <h2>Responses most similar to this signature</h2>
            <p>
              Each point represents a condition-aggregated measured signature for a reference
              compound. Distance reflects similarity in this transcriptomic feature space.
              Proximity does not prove the same mechanism, toxicity, or safety profile.
            </p>
          </div>
          {endpoints.length > 1 && (
            <label className="reference-context-select">
              Endpoint reference dataset
              <select value={context} onChange={(event) => { setContext(event.target.value); explore.reset(); }}>
                {endpoints.map((endpoint) => (
                  <option key={endpoint.endpoint_id} value={endpoint.endpoint_id}>
                    {endpoint.biological_target} ({endpoint.endpoint_id})
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
            <p>This stays empty until a real map is computed; no placeholder points are shown.</p>
          </div>
        )}
        {map.error != null && !notComputed && <ErrorNotice error={map.error} />}
        {map.data && (
          <>
            <ExploreScatter points={map.data.points} locate={explore.result} />
            <details className="technical-disclosure">
              <summary>Technical provenance</summary>
              <p>{map.data.manifest.point_definition}</p>
              <p>Source {map.data.manifest.source_key} · SHA-256 {map.data.manifest.source_sha256}</p>
            </details>
            <div className="result-reference-note">
              <span className="status-dot status-dot-blue" aria-hidden />
              Exact coordinates are used only for stored records; new uploads are placed approximately.
            </div>
          </>
        )}
      </section>

      <aside className="neighbor-panel">
        <p className="aside-label">Relative similarity</p>
        <h2>Nearest distinct signatures</h2>
        {explore.running && <p className="check-plain">Finding similar compounds…</p>}
        {explore.error != null && <ErrorNotice error={explore.error} />}
        {explore.result && (
          <>
            {explore.result.exact_match ? (
              <div className="exact-reference-match" data-testid="reference-exact-match">
                <strong>This measured signature is already present in the reference dataset.</strong>
                <p>Its stored coordinates are used and it is excluded from the neighbour list.</p>
                <p>
                  <strong>{explore.result.exact_match.preferred_name || "Compound name unavailable"}</strong>
                  {" · "}Condition-aggregated measured signature
                </p>
                {explore.result.exact_match.pubchem_cid && (
                  <p>PubChem CID {explore.result.exact_match.pubchem_cid}</p>
                )}
                {(explore.result.exact_match.experimental_contexts ?? []).map((item) => (
                  <p key={item}>{item}</p>
                ))}
              </div>
            ) : (
              <p className="check-plain" data-testid="reference-similarity">
                Approximate placement from nearest signatures in the full gene-expression space.
              </p>
            )}
            <ul className="reference-neighbors">
              {explore.result.neighbors.map((neighbor) => (
                <li key={neighbor.compound_id}>
                  <strong>{neighbor.preferred_name || "Compound name unavailable"}</strong>
                  <span>Condition-aggregated measured signature</span>
                  <span className="similarity-label">{neighbor.similarity_category || "similar response"}</span>
                  <span>
                    {ordinal(neighbor.similarity_rank)} closest among {map.data?.counts.n_total ?? "the"} reference signatures
                    {neighbor.similarity_percentile <= 0.01 ? " · Top 1% most similar" : ""}
                  </span>
                  {(neighbor.experimental_contexts ?? []).map((item) => <span key={item}>{item}</span>)}
                  {neighbor.source_dataset && <span>Source: {neighbor.source_dataset}</span>}
                  {neighbor.label && <span>Endpoint reference label: {neighbor.label}</span>}
                  <details>
                    <summary>Technical details</summary>
                    <span className="mono">InChIKey {neighbor.compound_id}</span>
                    <span>Raw Euclidean distance {neighbor.distance.toFixed(3)}</span>
                  </details>
                  {neighbor.full_signature_id ? (
                    <Link to={`/analyze?catalogue_signature=${encodeURIComponent(neighbor.full_signature_id)}`}>
                      Analyze this measured signature
                    </Link>
                  ) : (
                    <span>
                      This compound is included in the reference map, but its full gene-expression
                      vector is not available in the current public catalogue. It can be explored
                      here but cannot yet be re-analysed.
                    </span>
                  )}
                </li>
              ))}
            </ul>
            <p className="check-plain">
              Relative similarity is an empirical rank, not a probability or model confidence.
            </p>
          </>
        )}
        <Link className="detail-link" to="/explore">Open the full reference view</Link>
      </aside>
    </div>
  );
}

function ordinal(value: number): string {
  const mod100 = value % 100;
  const suffix = mod100 >= 11 && mod100 <= 13
    ? "th"
    : value % 10 === 1 ? "st" : value % 10 === 2 ? "nd" : value % 10 === 3 ? "rd" : "th";
  return `${value}${suffix}`;
}
