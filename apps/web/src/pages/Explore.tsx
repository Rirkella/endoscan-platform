// Reference data — "Explore measured toxicology responses". Two user tasks: (A) explore the
// reference landscape, (B) place a measured signature in it. Honesty-first by construction:
//   - a point is a measured REFERENCE SIGNATURE (one per compound, by InChIKey), not proof of an
//     effect; the map is a visual guide, not a model boundary;
//   - active/inactive are ENDPOINT-DATASET labels, not a universal safe/harmful statement;
//   - a placed signature is APPROXIMATE (nearest neighbours in the feature space, no exact
//     projection) and there is no in-/out-of-domain verdict in the visible layer;
//   - all technical detail (metric, percentile, UMAP params, provenance) lives in a collapsed
//     "Technical details" disclosure so the main view stays plain and biological.
// Input uses the same server-validated upload path as Analyze; raw JSON remains advanced.

import { type FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { EndoscanApiError, api } from "../api/client";
import type {
  CatalogueCompound,
  ExplorePoint,
  ParseResult,
  Signature,
} from "../api/types";
import { ErrorNotice } from "../components/ErrorNotice";
import { ExploreScatter } from "../components/ExploreScatter";
import { useAsync } from "../hooks/useAsync";
import { useExplore } from "../hooks/useExplore";

type LabelFilter = "all" | "active" | "inactive" | "unlabeled";

interface SearchMatch {
  compoundId: string;
  name: string | null;
  status: string;
  reason: string | null;
  referenceContexts: string[];
  inSelectedMap: boolean;
}
function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="legend-item">
      <span className="legend-swatch" style={{ backgroundColor: color }} />
      {label}
    </span>
  );
}

export function Explore() {
  const navigate = useNavigate();
  const endpoints = useAsync(() => api.listEndpoints(), []);
  const [context, setContext] = useState<string | null>(null);
  const [labelFilter, setLabelFilter] = useState<LabelFilter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeNeighborId, setActiveNeighborId] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [selectedCompound, setSelectedCompound] = useState<CatalogueCompound | null>(null);
  const [selectedLoading, setSelectedLoading] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchMatches, setSearchMatches] = useState<SearchMatch[] | null>(null);
  const [searchError, setSearchError] = useState<unknown>(null);

  useEffect(() => {
    if (context == null && (endpoints.data?.length ?? 0) > 0) {
      setContext(endpoints.data![0].endpoint_id);
    }
  }, [endpoints.data, context]);

  const map = useAsync(
    () => (context ? api.exploreUmap(context) : Promise.reject(new Error("no context"))),
    [context],
  );
  const explore = useExplore(context);

  useEffect(() => {
    let current = true;
    setSelectedCompound(null);
    if (!selectedId) return () => { current = false; };
    setSelectedLoading(true);
    void api
      .searchCatalogue(selectedId, 20)
      .then((response) => {
        if (!current) return;
        setSelectedCompound(
          response.results.find((item) => item.compound_id === selectedId) ?? null,
        );
      })
      .catch(() => {
        if (current) setSelectedCompound(null);
      })
      .finally(() => {
        if (current) setSelectedLoading(false);
      });
    return () => { current = false; };
  }, [selectedId]);

  async function searchReference(event: FormEvent) {
    event.preventDefault();
    const query = searchQuery.trim();
    if (query.length < 2 || !map.data) return;
    setSearching(true);
    setSearchError(null);
    const mapIds = new Set(map.data.points.map((point) => point.compound_id));
    const byId = map.data.points
      .filter((point) => point.compound_id.toLowerCase().includes(query.toLowerCase()))
      .slice(0, 10)
      .map((point) => ({ compoundId: point.compound_id, name: point.preferred_name, status: "Reference profile available", reason: null, referenceContexts: [map.data!.context], inSelectedMap: true }));
    try {
      const response = await api.searchCatalogue(query, 20);
      const named = response.results.map((compound) => ({
          compoundId: compound.compound_id,
          name: compound.preferred_name,
          status: compound.reference_contexts.includes(map.data!.context)
            ? compound.availability_status
            : compound.reference_contexts.length
              ? "Present in another endpoint reference set"
              : compound.availability_status,
          reason: compound.availability_reason,
          referenceContexts: compound.reference_contexts,
          inSelectedMap: mapIds.has(compound.compound_id),
        }));
      const seen = new Set(named.map((match) => match.compoundId));
      setSearchMatches([...named, ...byId.filter((match) => !seen.has(match.compoundId))]);
    } catch (error) {
      setSearchMatches(byId);
      setSearchError(error);
    } finally {
      setSearching(false);
    }
  }

  const notComputed = map.error instanceof EndoscanApiError && map.error.status === 404;
  const labelled =
    map.data != null &&
    map.data.manifest.label_status !== "none" &&
    map.data.manifest.label_status !== "unjoined";

  return (
    <div className="reference-page">
      <div className="page-header">
        <div>
          <p className="eyebrow">Reference data</p>
          <h1>Explore measured toxicology responses</h1>
          <p className="page-copy">
            This map groups experiments with similar gene-expression responses. Nearby points show
            similar response patterns — not necessarily the same toxicological effect.
          </p>
        </div>
      </div>

      <div className="reference-toolbar">
        <label>
          Endpoint
          <select
            value={context ?? ""}
            onChange={(e) => {
              setContext(e.target.value);
              explore.reset();
              setSelectedId(null);
              setActiveNeighborId(null);
              setFocusId(null);
              setLabelFilter("all");
              setSearchMatches(null);
            }}
          >
            {(endpoints.data ?? []).map((e) => (
              <option key={e.endpoint_id} value={e.endpoint_id}>
                {e.endpoint_id} — {e.biological_target}
              </option>
            ))}
          </select>
        </label>
        {map.data && (
          <div className="reference-summary">
            <div>
              <strong className="tabular">{map.data.counts.n_total}</strong>
              <span>reference signatures</span>
            </div>
            {labelled && (
              <>
                <div>
                  <strong className="tabular">{map.data.counts.n_active}</strong>
                  <span>labelled active</span>
                </div>
                <div>
                  <strong className="tabular">{map.data.counts.n_inactive}</strong>
                  <span>labelled inactive</span>
                </div>
              </>
            )}
            <div>
              <strong>{map.data.manifest.target}</strong>
              <span>endpoint dataset</span>
            </div>
          </div>
        )}
      </div>

      <div className="reference-explorer-controls">
        <form className="reference-search-form" onSubmit={(event) => void searchReference(event)}>
          <label>
            Find a reference signature
            <input
              value={searchQuery}
              onChange={(event) => setSearchQuery(event.target.value)}
              placeholder="Available name or InChIKey"
              aria-label="Search reference signatures"
            />
          </label>
          <button
            className="button outline"
            type="submit"
            disabled={!map.data || searching || searchQuery.trim().length < 2}
          >
            {searching ? "Searching…" : "Find"}
          </button>
        </form>
        <label className="reference-filter">
          Dataset class
          <select
            value={labelFilter}
            onChange={(event) => setLabelFilter(event.target.value as LabelFilter)}
          >
            <option value="all">All available labels</option>
            <option value="active">Labelled active</option>
            <option value="inactive">Labelled inactive</option>
            {map.data?.counts.n_unlabeled ? <option value="unlabeled">Unlabelled</option> : null}
          </select>
        </label>
      </div>
      {searchError != null && <ErrorNotice error={searchError} />}
      {searchMatches != null && (
        <div className="reference-search-results" aria-live="polite">
          {searchMatches.length === 0 ? (
            <p>No known compound identity or reference record matched this search.</p>
          ) : (
            searchMatches.map((match) => (
              <button
                key={match.compoundId}
                type="button"
                onClick={() => match.inSelectedMap && setSelectedId(match.compoundId)}
                aria-disabled={!match.inSelectedMap}
              >
                <strong>{match.name ?? "Reference signature"}</strong>
                <span className="mono">{match.compoundId}</span>
                <span className="search-availability">{match.status}</span>
                {match.reason && <small>{match.reason}</small>}
              </button>
            ))
          )}
        </div>
      )}

      {endpoints.error != null && <ErrorNotice error={endpoints.error} />}

      <div className="reference-context-layout">
        {/* A — the reference landscape */}
        <section className="reference-map-card">
          {(map.loading || context == null) && (
            <p className="check-plain">Loading the reference landscape…</p>
          )}

          {notComputed && context != null && (
            <div className="no-signature">
              <strong>No reference map for {context} yet</strong>
              <p>
                This view stays empty until a real map is computed on the server — no placeholder
                points are shown.
              </p>
            </div>
          )}

          {map.error != null && !notComputed && context != null && <ErrorNotice error={map.error} />}

          {map.data && (
            <>
              <ExploreScatter
                points={map.data.points}
                locate={explore.result}
                selectedId={selectedId}
                labelFilter={labelFilter}
                pointNames={
                  selectedCompound
                    ? { [selectedCompound.compound_id]: selectedCompound.preferred_name }
                    : undefined
                }
                onSelect={(point) => setSelectedId(point.compound_id)}
                activeNeighborId={activeNeighborId}
                focusId={focusId}
                onNeighborHover={setActiveNeighborId}
              />
              <div className="reference-legend">
                {labelled ? (
                  <>
                    {map.data.counts.n_active > 0 && (
                      <LegendDot color="#b45309" label="Labelled active for this endpoint" />
                    )}
                    {map.data.counts.n_inactive > 0 && (
                      <LegendDot color="#475569" label="Labelled inactive for this endpoint" />
                    )}
                    {map.data.counts.n_unlabeled > 0 && (
                      <LegendDot color="#cbd5e1" label="No label in this dataset" />
                    )}
                  </>
                ) : (
                  <span className="legend-item">Uncoloured — no reliable labels joined</span>
                )}
                {explore.result && <LegendDot color="#7c3aed" label="Your signature (approximate)" />}
              </div>
              <div className="reference-label-note">
                These labels describe the reference endpoint dataset. They are not a general
                statement that a compound is safe or harmful. The map is a visual guide — not a model
                boundary, and not proof of a shared effect.
              </div>
            </>
          )}
        </section>

        {/* B — place a measured signature */}
        {selectedId && map.data ? (
          <ReferenceDetailsPanel
            point={map.data.points.find((item) => item.compound_id === selectedId) ?? null}
            compound={selectedCompound}
            loading={selectedLoading}
            context={context ?? map.data.context}
            onClose={() => setSelectedId(null)}
            onAnalyze={() => navigate(`/analyze?reference_context=${encodeURIComponent(context ?? map.data!.context)}&reference_compound=${encodeURIComponent(selectedId)}`)}
          />
        ) : (
        <aside className="reference-place">
          <p className="aside-label">Place a signature</p>
          <p className="check-plain">
            Bring a measured signature to find the most similar reference signatures. Its position is{" "}
            <strong>approximate</strong>.
          </p>

          <PlacementInput
            disabled={explore.running || !context}
            onSignature={(sig) => explore.locate(sig)}
          />

          {explore.running && <p className="check-plain">Finding similar signatures…</p>}
          {explore.error != null && <ErrorNotice error={explore.error} />}

          {explore.result && (
            <div className="reference-result-block">
              {explore.result.exact_match ? (
                <p className="check-plain" data-testid="similarity-readout">
                  <strong>This measured signature is already present in the reference dataset.</strong>{" "}
                  Its stored coordinates are used and the self-match is excluded below.
                </p>
              ) : (
                <p className="check-plain" data-testid="similarity-readout">
                  Approximate position based on nearest measured signatures in the full feature space.
                </p>
              )}

              <p className="aside-label" style={{ marginTop: "16px" }}>
                Most similar full gene-expression profiles
              </p>
              <p className="check-plain">
                Similar response patterns. This does not prove the same effect.
              </p>
              <p className="map-distance-note">The neighbour list is ranked using the complete 978-gene profiles. The 2D map is a visual approximation, so apparent distance on the map may differ from the full-profile ranking.</p>
              <ol className="reference-neighbors numbered-neighbors" data-testid="similar-compounds">
                {explore.result.neighbors.slice(0, 5).map((n, index) => (
                  <li key={n.compound_id} onMouseEnter={() => setActiveNeighborId(n.compound_id)} onMouseLeave={() => setActiveNeighborId(null)}>
                    <button type="button" className="neighbor-focus" onClick={() => setFocusId(n.compound_id)}><span>{index + 1}.</span><strong>{n.preferred_name ?? "Compound name unavailable"}</strong></button>
                    {n.source_dataset && <span>{n.source_dataset}</span>}
                    {n.label && <span className={`endpoint-reference-badge endpoint-${n.label}`}>{context}: {n.label === "active" ? "Active" : "Inactive"}</span>}
                    <details>
                      <summary>Technical details</summary>
                      <span className="mono">InChIKey {n.compound_id}</span>
                      <span>Raw distance {n.distance.toFixed(3)}</span>
                    </details>
                  </li>
                ))}
              </ol>
              <p className="endpoint-label-caveat">Active and inactive refer only to the selected endpoint reference dataset.</p>

              <details
                data-testid="explore-technical"
                className="reference-technical"
              >
                <summary>Technical details</summary>
                <dl>
                  <div>
                    <dt>Similarity metric</dt>
                    <dd>
                      distance to the {explore.result.domain.k}-th nearest reference signature ={" "}
                      {explore.result.domain.query_kth_distance.toFixed(3)} (
                      {explore.result.domain.metric}); typical training range p25–p95:{" "}
                      {explore.result.domain.training_reference_quantiles.p25?.toFixed(3)}–
                      {explore.result.domain.training_reference_quantiles.p95?.toFixed(3)}.
                    </dd>
                  </div>
                  <div>
                    <dt>Percentile → band</dt>
                    <dd>
                      isolation percentile ={" "}
                      <strong>{(explore.result.domain.percentile * 100).toFixed(1)}%</strong>. This
                      diagnostic is separate from neighbour similarity ranks and does not assert
                      in-domain or out-of-domain status.
                    </dd>
                  </div>
                  <div>
                    <dt>Feature space</dt>
                    <dd>
                      landmark-gene expression space (the endpoint&rsquo;s feature schema); k ={" "}
                      {explore.result.domain.k}.
                    </dd>
                  </div>
                  {map.data && (
                    <>
                      <div>
                        <dt>Projection</dt>
                        <dd>
                          UMAP (n_neighbors={String(map.data.manifest.umap.n_neighbors)}, min_dist=
                          {String(map.data.manifest.umap.min_dist)}, metric=
                          {String(map.data.manifest.umap.metric)}, random_state=
                          {String(map.data.manifest.umap.random_state)}, v
                          {String(map.data.manifest.umap.umap_version)}). Visualization only.
                        </dd>
                      </div>
                      <div>
                        <dt>Reference dataset</dt>
                        <dd>
                          {map.data.manifest.n_compounds} signatures ({context}); labels:{" "}
                          {map.data.manifest.label_status}; source LINCS L1000; built{" "}
                          {map.data.manifest.built_at ?? "unknown"}.
                        </dd>
                      </div>
                      <div>
                        <dt>What each point represents</dt>
                        <dd>
                          {map.data.manifest.point_definition}. Condition selection:{" "}
                          {map.data.manifest.aggregation.condition_rule ?? "recorded in the build pipeline"};
                          cell-line fusion:{" "}
                          {map.data.manifest.aggregation.cell_line_fusion ?? "recorded in the build pipeline"}.
                        </dd>
                      </div>
                    </>
                  )}
                </dl>
              </details>
            </div>
          )}
        </aside>
        )}
      </div>
    </div>
  );
}

function ReferenceDetailsPanel({
  point,
  compound,
  loading,
  context,
  onClose,
  onAnalyze,
}: {
  point: ExplorePoint | null;
  compound: CatalogueCompound | null;
  loading: boolean;
  context: string;
  onClose: () => void;
  onAnalyze: () => void;
}) {
  if (!point) return null;
  const pubchemCid = compound?.pubchem_cid ?? point.pubchem_cid;
  return (
    <aside
      className="reference-details-drawer"
      role="region"
      aria-label="Reference signature details"
    >
      <div className="reference-drawer-head">
        <div>
          <p className="eyebrow">Selected compound</p>
          <h2>{compound?.preferred_name ?? point.preferred_name ?? "Compound name unavailable"}</h2>
          <p>Aggregated reference profile</p>
        </div>
        <button
          type="button"
          className="button quiet"
          onClick={onClose}
          aria-label="Close details"
        >
          Close
        </button>
      </div>
      {pubchemCid && <img className="compound-structure" src={`https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/${pubchemCid}/PNG?record_type=2d`} alt={`2D chemical structure for ${compound?.preferred_name ?? point.preferred_name ?? point.compound_id}`} />}
      <p className="reference-profile-explanation">This profile combines the measured experimental conditions selected for this compound when the reference map was built.</p>
      <dl className="reference-drawer-list">
        {(compound?.pubchem_cid ?? point.pubchem_cid) && (
          <div><dt>PubChem CID</dt><dd>{compound?.pubchem_cid ?? point.pubchem_cid}</dd></div>
        )}
        <div>
          <dt>{context} reference</dt>
          <dd>{point.label ? <span className={`endpoint-reference-badge endpoint-${point.label}`}>{context}: {point.label === "active" ? "Active" : "Inactive"}</span> : "No label in this endpoint dataset"}</dd>
        </div>
        <div><dt>Gene-expression measurements</dt><dd>LINCS L1000, Level 5</dd></div>
        <div><dt>Cell models</dt><dd>MCF7 — human breast cancer cell line<br />A549 — human lung adenocarcinoma cell line</dd></div>
        {(point.experimental_contexts ?? []).length > 0 && (
          <div><dt>Experimental context</dt><dd>{(point.experimental_contexts ?? []).join("; ")}</dd></div>
        )}
      </dl>
      <p className="reference-label-note">
        Proximity does not prove a shared mechanism, toxicity, or safety profile. A submitted query
        is placed approximately; this selected reference point is part of the precomputed map.
      </p>
      {loading ? (
        <p className="check-plain">Preparing reference actions…</p>
      ) : (
        <div className="reference-drawer-action">
          <button
            className="button primary"
            type="button"
            onClick={onAnalyze}
          >
            Analyze this aggregated reference profile
          </button>
          <button className="button outline" type="button" disabled={!point.underlying_condition_available}>View underlying experiments</button>
          {pubchemCid && <a className="detail-link" href={`https://pubchem.ncbi.nlm.nih.gov/compound/${pubchemCid}`} target="_blank" rel="noreferrer">Open source record</a>}
          {!point.underlying_condition_available && <p>Condition-specific measurements are not committed in this demonstrator; only the exact aggregate support vector is available.</p>}
        </div>
      )}
    </aside>
  );
}

// Placement input: upload first; raw JSON is demoted to an advanced disclosure.
function PlacementInput({
  disabled,
  onSignature,
}: {
  disabled?: boolean;
  onSignature: (sig: Signature) => void;
}) {
  const [uploadError, setUploadError] = useState<unknown>(null);
  const [pasteText, setPasteText] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);

  async function onFile(f: File | null) {
    setUploadError(null);
    if (!f) return;
    const n = f.name.toLowerCase();
    const fmt = n.endsWith(".json") ? "json" : n.endsWith(".csv") ? "csv" : n.endsWith(".tsv") ? "tsv" : null;
    if (!fmt) {
      setUploadError(new Error("Please upload a .json, .csv or .tsv file."));
      return;
    }
    try {
      const r: ParseResult = await api.parseSignature({ file: f, format: fmt });
      if (r.ready && r.signature) onSignature(r.signature);
      else setUploadError(new Error("The file could not be prepared for reference placement."));
    } catch (e) {
      setUploadError(e);
    }
  }

  function loadPaste() {
    try {
      const parsed = JSON.parse(pasteText) as unknown;
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Signature must be a JSON object of gene symbol → number.");
      }
      const out: Signature = {};
      for (const [g, v] of Object.entries(parsed as Record<string, unknown>)) {
        if (typeof v !== "number" || !Number.isFinite(v)) throw new Error(`Gene "${g}" must be a number.`);
        out[g] = v;
      }
      if (Object.keys(out).length === 0) throw new Error("Signature is empty.");
      setPasteError(null);
      onSignature(out);
    } catch (e) {
      setPasteError(e instanceof Error ? e.message : "Invalid JSON.");
    }
  }

  return (
    <div className="placement-input">
      <label className="placement-field">
        <span>Upload a signature file</span>
        <input
          type="file"
          accept=".json,.csv,.tsv"
          disabled={disabled}
          onChange={(e) => void onFile(e.target.files?.[0] ?? null)}
        />
      </label>
      <p className="placement-note">
        Endpoint compatibility is checked by the server when the signature is placed.
      </p>
      {uploadError != null && <ErrorNotice error={uploadError} />}

      <details className="reference-advanced">
        <summary>Advanced technical input</summary>
        <label className="advanced-field">
          <span>Signature (JSON: gene symbol → finite numeric value)</span>
          <textarea
            value={pasteText}
            placeholder='{ "A1BG": 0.12, "…": 0.0 }'
            disabled={disabled}
            onChange={(e) => setPasteText(e.target.value)}
          />
        </label>
        {pasteError && (
          <p role="alert" className="paste-error">
            {pasteError}
          </p>
        )}
        <button
          className="button outline"
          disabled={disabled || pasteText.trim().length === 0}
          onClick={loadPaste}
        >
          Load signature
        </button>
      </details>
    </div>
  );
}
