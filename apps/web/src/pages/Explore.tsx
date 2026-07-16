// Reference data — "Explore measured toxicology responses". Two user tasks: (A) explore the
// reference landscape, (B) place a measured signature in it. Honesty-first by construction:
//   - a point is a measured REFERENCE SIGNATURE (one per compound, by InChIKey), not proof of an
//     effect; the map is a visual guide, not a model boundary;
//   - active/inactive are ENDPOINT-DATASET labels, not a universal safe/harmful statement;
//   - a placed signature is APPROXIMATE (nearest neighbours in the feature space, no exact
//     projection) and there is no in-/out-of-domain verdict in the visible layer;
//   - all technical detail (metric, percentile, UMAP params, provenance) lives in a collapsed
//     "Technical details" disclosure so the main view stays plain and biological.
// Input is demo / upload first; raw JSON is demoted into "Advanced technical input".

import { type FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { EndoscanApiError, api } from "../api/client";
import type {
  CatalogueCompound,
  ExplorePoint,
  ParseResult,
  Signature,
} from "../api/types";
import { demoSignatures } from "../demo-signatures";
import { demoDisplay } from "../demo-signatures/display";
import { ErrorNotice } from "../components/ErrorNotice";
import { ExploreScatter } from "../components/ExploreScatter";
import { useAsync } from "../hooks/useAsync";
import { useExplore } from "../hooks/useExplore";

type LabelFilter = "all" | "active" | "inactive" | "unlabeled";

interface SearchMatch {
  compoundId: string;
  name: string | null;
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
      .map((point) => ({ compoundId: point.compound_id, name: null }));
    try {
      const response = await api.searchCatalogue(query, 20);
      const named = response.results
        .filter((compound) => mapIds.has(compound.compound_id))
        .map((compound) => ({
          compoundId: compound.compound_id,
          name: compound.preferred_name,
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
            <p>No matching measured signature exists in this endpoint map.</p>
          ) : (
            searchMatches.map((match) => (
              <button
                key={match.compoundId}
                type="button"
                onClick={() => setSelectedId(match.compoundId)}
              >
                <strong>{match.name ?? "Reference signature"}</strong>
                <span className="mono">{match.compoundId}</span>
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
                Nearest reference signatures
              </p>
              <p className="check-plain">
                Similar response patterns. This does not prove the same effect.
              </p>
              <ul className="reference-neighbors" data-testid="similar-compounds">
                {explore.result.neighbors.map((n) => (
                  <li key={n.compound_id}>
                    <strong>{n.preferred_name ?? "Compound name unavailable"}</strong>
                    <span>{n.similarity_category} · rank {n.similarity_rank} of {map.data?.counts.n_total}</span>
                    {n.source_dataset && <span>{n.source_dataset}</span>}
                    {n.label && <span>endpoint label: {n.label}</span>}
                    <details>
                      <summary>Technical details</summary>
                      <span className="mono">InChIKey {n.compound_id}</span>
                      <span>Raw distance {n.distance.toFixed(3)}</span>
                    </details>
                  </li>
                ))}
              </ul>

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
                          {map.data.manifest.label_status}; source{" "}
                          {map.data.manifest.source_key || "curated endpoint signatures"} ({map.data.manifest.source_sha256.slice(0, 12)}); built{" "}
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
      </div>

      {selectedId && map.data && (
        <ReferenceDetailsDrawer
          point={map.data.points.find((item) => item.compound_id === selectedId) ?? null}
          compound={selectedCompound}
          loading={selectedLoading}
          context={context ?? map.data.context}
          sourceKey={map.data.manifest.source_key}
          sourceHash={map.data.manifest.source_sha256}
          pointDefinition={map.data.manifest.point_definition}
          onClose={() => setSelectedId(null)}
          onAnalyze={(signatureId) =>
            navigate(`/analyze?catalogue_signature=${encodeURIComponent(signatureId)}`)
          }
        />
      )}
    </div>
  );
}

function ReferenceDetailsDrawer({
  point,
  compound,
  loading,
  context,
  sourceKey,
  sourceHash,
  pointDefinition,
  onClose,
  onAnalyze,
}: {
  point: ExplorePoint | null;
  compound: CatalogueCompound | null;
  loading: boolean;
  context: string;
  sourceKey: string;
  sourceHash: string;
  pointDefinition: string;
  onClose: () => void;
  onAnalyze: (signatureId: string) => void;
}) {
  if (!point) return null;
  const signature = compound?.signatures[0];
  return (
    <aside
      className="reference-details-drawer"
      role="dialog"
      aria-label="Reference signature details"
    >
      <div className="reference-drawer-head">
        <div>
          <p className="eyebrow">Selected measured signature</p>
          <h2>{compound?.preferred_name ?? point.preferred_name ?? "Compound name unavailable"}</h2>
          <p>Condition-aggregated measured signature</p>
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
      <dl className="reference-drawer-list">
        {(compound?.pubchem_cid ?? point.pubchem_cid) && (
          <div><dt>PubChem CID</dt><dd>{compound?.pubchem_cid ?? point.pubchem_cid}</dd></div>
        )}
        <div>
          <dt>Endpoint dataset label</dt>
          <dd>{point.label ? `${point.label} for ${context}` : "No label in this endpoint dataset"}</dd>
        </div>
        <div><dt>Source dataset</dt><dd>{point.source_dataset || sourceKey || "Curated endpoint signatures"}</dd></div>
        {(point.experimental_contexts ?? []).length > 0 && (
          <div><dt>Experimental context</dt><dd>{(point.experimental_contexts ?? []).join("; ")}</dd></div>
        )}
      </dl>
      <details className="technical-disclosure">
        <summary>Technical provenance</summary>
        <dl className="reference-drawer-list">
          <div><dt>InChIKey</dt><dd className="mono">{point.compound_id}</dd></div>
          <div><dt>Point definition</dt><dd>{pointDefinition}</dd></div>
          <div><dt>UMAP coordinates</dt><dd>{point.x.toFixed(3)}, {point.y.toFixed(3)}</dd></div>
          <div><dt>Artifact</dt><dd>{sourceKey}; SHA-256 {sourceHash}</dd></div>
        </dl>
      </details>
      <p className="reference-label-note">
        Proximity does not prove a shared mechanism, toxicity, or safety profile. A submitted query
        is placed approximately; this selected reference point is part of the precomputed map.
      </p>
      {loading ? (
        <p className="check-plain">Checking for an analyzable public record…</p>
      ) : signature ? (
        <div className="reference-drawer-action">
          <p>{signature.dataset} {signature.processing_level} · {signature.cell_lines.join(" + ")}</p>
          <button
            className="button primary"
            type="button"
            onClick={() => onAnalyze(signature.signature_id)}
          >
            Analyze this measured signature
          </button>
        </div>
      ) : (
        <p className="check-plain">
          This compound is included in the reference map, but its full gene-expression vector is
          not available in the current public catalogue. It can be explored here but cannot yet be
          re-analysed. A transcriptomic signature is never reconstructed from 2D UMAP coordinates.
        </p>
      )}
    </aside>
  );
}

// Placement input: demo (real, one click) and upload first; raw JSON demoted to an advanced
// disclosure so it never dominates the page.
function PlacementInput({
  disabled,
  onSignature,
}: {
  disabled?: boolean;
  onSignature: (sig: Signature) => void;
}) {
  const [demoId, setDemoId] = useState("");
  const [uploadError, setUploadError] = useState<unknown>(null);
  const [pasteText, setPasteText] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);

  function pickDemo(id: string) {
    setDemoId(id);
    const demo = demoSignatures.find((d) => d.id === id);
    if (demo) onSignature(demo.signature);
  }

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
      {demoSignatures.length > 0 && (
        <label className="placement-field">
          <span>Choose a real demo signature</span>
          <select value={demoId} disabled={disabled} onChange={(e) => pickDemo(e.target.value)}>
            <option value="">Select a demo…</option>
            {demoSignatures.map((d) => {
              const dd = demoDisplay(d);
              return (
                <option key={d.id} value={d.id}>
                  {dd.name}
                </option>
              );
            })}
          </select>
        </label>
      )}

      <label className="placement-field">
        <span>Or upload a signature file</span>
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
