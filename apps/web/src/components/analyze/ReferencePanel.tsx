import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { EndoscanApiError, api } from "../../api/client";
import type { EndpointSummary, ExploreLocateResult, ExploreNeighbor, Signature } from "../../api/types";
import { useAsync } from "../../hooks/useAsync";
import { useExplore } from "../../hooks/useExplore";
import { ErrorNotice } from "../ErrorNotice";
import { ExploreScatter } from "../ExploreScatter";

export function ReferencePanel({ endpoints, signature }: { endpoints: EndpointSummary[]; signature: Signature }) {
  const [context, setContext] = useState(endpoints[0]?.endpoint_id ?? "");
  const [activeNeighborId, setActiveNeighborId] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [selectedNeighborId, setSelectedNeighborId] = useState<string | null>(null);
  const map = useAsync(() => context ? api.exploreUmap(context) : Promise.reject(new Error("no context")), [context]);
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
            <p>These compounds produced gene-expression patterns most similar to the current signature in the selected endpoint reference set. Similarity does not prove the same biological mechanism or toxicological effect.</p>
          </div>
          {endpoints.length > 1 && (
            <label className="reference-context-select">
              Endpoint reference set
              <select value={context} onChange={(event) => { setContext(event.target.value); explore.reset(); setFocusId(null); setSelectedNeighborId(null); }}>
                {endpoints.map((endpoint) => <option key={endpoint.endpoint_id} value={endpoint.endpoint_id}>{endpoint.biological_target} ({endpoint.endpoint_id})</option>)}
              </select>
            </label>
          )}
        </div>
        {(map.loading || !context) && <p className="check-plain">Loading the reference landscape…</p>}
        {notComputed && <div className="no-signature"><strong>No reference map for {context} yet</strong></div>}
        {map.error != null && !notComputed && <ErrorNotice error={map.error} />}
        {map.data && <ExploreScatter
          points={map.data.points}
          locate={explore.result}
          activeNeighborId={activeNeighborId}
          focusId={focusId}
          selectedId={selectedNeighborId}
          onNeighborHover={setActiveNeighborId}
          onSelect={(point) => {
            setFocusId(point.compound_id);
            setSelectedNeighborId(point.compound_id);
          }}
        />}
        <p className="map-distance-note">The neighbour list is ranked using the complete 978-gene profiles. The 2D map is a visual approximation, so apparent distance on the map may differ from the full-profile ranking.</p>
      </section>

      <aside className="neighbor-panel">
        <p className="aside-label">Relative similarity</p>
        <h2>Most similar full gene-expression profiles</h2>
        <p className="check-plain">Reference records aggregate measured conditions; context is shown only when available.</p>
        {explore.running && <p className="check-plain">Finding similar compounds…</p>}
        {explore.error != null && <ErrorNotice error={explore.error} />}
        {explore.result && <NeighborResults
          result={explore.result}
          context={context}
          selectedId={selectedNeighborId}
          onHover={setActiveNeighborId}
          onFocus={(id) => {
            setFocusId(id);
            setSelectedNeighborId(id);
          }}
        />}
        <Link className="detail-link" to="/explore">Open the full reference view</Link>
      </aside>
    </div>
  );
}

function NeighborResults({ result, context, selectedId, onHover, onFocus }: {
  result: ExploreLocateResult;
  context: string;
  selectedId: string | null;
  onHover: (id: string | null) => void;
  onFocus: (id: string) => void;
}) {
  const named = result.neighbors.filter((neighbor) => neighbor.preferred_name).slice(0, 5);
  const unresolved = result.neighbors.filter((neighbor) => !neighbor.preferred_name);
  const closerUnresolved = named.length
    ? unresolved.filter((neighbor) => neighbor.similarity_rank < named[0].similarity_rank).length
    : unresolved.length;
  const selected = result.neighbors.find((neighbor) => neighbor.compound_id === selectedId) ?? null;
  return (
    <>
      {result.exact_match && (
        <div className="exact-reference-match" data-testid="reference-exact-match">
          <strong>Exact measured reference match</strong>
          <p>{result.exact_match.preferred_name || "Identity unresolved"}</p>
        </div>
      )}
      {closerUnresolved > 0 && <p className="unresolved-note">{closerUnresolved} closer reference {closerUnresolved === 1 ? "record could" : "records could"} not be resolved to a public compound name.</p>}
      {selected && (
        <section className="selected-neighbor-summary" aria-label="Selected neighbour compound summary">
          <p className="aside-label">Selected map point</p>
          <strong>{selected.preferred_name ?? "Identity unresolved"}</strong>
          <span>Aggregated reference profile</span>
          {selected.label && <span className={`endpoint-reference-badge endpoint-${selected.label}`}>{context}: {selected.label === "active" ? "Active" : "Inactive"}</span>}
          {selected.source_dataset && <span>{selected.source_dataset}</span>}
          {selected.full_vector_available && <Link to={`/analyze?reference_context=${encodeURIComponent(context)}&reference_compound=${encodeURIComponent(selected.compound_id)}`}>Analyze this aggregated reference profile</Link>}
        </section>
      )}
      {named.length > 0 ? (
        <ol className="reference-neighbors numbered-neighbors">
          {named.map((neighbor, index) => <NamedNeighbor key={neighbor.compound_id} neighbor={neighbor} number={index + 1} context={context} onHover={onHover} onFocus={onFocus} />)}
        </ol>
      ) : <p className="check-plain">No named neighbour is available in this result.</p>}
      {unresolved.length > 0 && (
        <details className="unresolved-references">
          <summary>{unresolved.length} unresolved reference {unresolved.length === 1 ? "record" : "records"}</summary>
          <ol>{unresolved.map((neighbor) => <li key={neighbor.compound_id}>{ordinal(neighbor.similarity_rank)} closest · identity unresolved</li>)}</ol>
        </details>
      )}
      <p className="check-plain">Relative similarity is an empirical full-profile rank, not a probability or model confidence.</p>
      <p className="endpoint-label-caveat">Active and inactive refer only to the selected endpoint reference dataset.</p>
    </>
  );
}

function NamedNeighbor({ neighbor, number, context, onHover, onFocus }: {
  neighbor: ExploreNeighbor;
  number: number;
  context: string;
  onHover: (id: string | null) => void;
  onFocus: (id: string) => void;
}) {
  return (
    <li onMouseEnter={() => onHover(neighbor.compound_id)} onMouseLeave={() => onHover(null)} data-neighbor-row={neighbor.compound_id}>
      <button type="button" className="neighbor-focus" onClick={() => onFocus(neighbor.compound_id)}><span>{number}.</span><strong>{neighbor.preferred_name}</strong></button>
      {neighbor.label && <span className={`endpoint-reference-badge endpoint-${neighbor.label}`}>{context}: {neighbor.label === "active" ? "Active" : "Inactive"}</span>}
      {(neighbor.experimental_contexts ?? []).slice(0, 2).map((item) => <span key={item}>{item}</span>)}
      {neighbor.full_vector_available && <Link to={`/analyze?reference_context=${encodeURIComponent(context)}&reference_compound=${encodeURIComponent(neighbor.compound_id)}`}>Analyze this aggregated reference profile</Link>}
    </li>
  );
}

function ordinal(value: number): string {
  const mod100 = value % 100;
  const suffix = mod100 >= 11 && mod100 <= 13 ? "th" : value % 10 === 1 ? "st" : value % 10 === 2 ? "nd" : value % 10 === 3 ? "rd" : "th";
  return `${value}${suffix}`;
}
