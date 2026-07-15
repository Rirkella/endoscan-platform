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

import { useEffect, useState } from "react";

import { EndoscanApiError, api } from "../api/client";
import type { ParseResult, Signature } from "../api/types";
import { demoSignatures } from "../demo-signatures";
import { demoDisplay } from "../demo-signatures/display";
import { ErrorNotice } from "../components/ErrorNotice";
import { ExploreScatter } from "../components/ExploreScatter";
import { useAsync } from "../hooks/useAsync";
import { useExplore } from "../hooks/useExplore";

type SimilarityBand = "close" | "moderately close" | "far";
function similarityBand(percentile: number): SimilarityBand {
  if (percentile <= 0.5) return "close";
  if (percentile <= 0.9) return "moderately close";
  return "far";
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
  const endpoints = useAsync(() => api.listEndpoints(), []);
  const [context, setContext] = useState<string | null>(null);

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
              <ExploreScatter points={map.data.points} locate={explore.result} />
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
              <p className="check-plain" data-testid="similarity-readout">
                Approximate position based on the most similar reference signatures. This signature
                is <strong>{similarityBand(explore.result.domain.percentile)}</strong> to
                EndoScan&rsquo;s reference data — a graded comparison, not a pass or fail.
              </p>

              <p className="aside-label" style={{ marginTop: "16px" }}>
                Nearest reference signatures
              </p>
              <p className="check-plain">
                Similar response patterns. This does not prove the same effect.
              </p>
              <ul className="reference-neighbors" data-testid="similar-compounds">
                {explore.result.neighbors.map((n) => (
                  <li key={n.compound_id}>
                    <span className="mono">{n.compound_id}</span>
                    {n.label && <span>labelled {n.label}</span>}
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
                      percentile ={" "}
                      <strong>{(explore.result.domain.percentile * 100).toFixed(1)}%</strong> of
                      training compounds are at least this isolated. Bands: ≤50% → close, 50–90% →
                      moderately close, &gt;90% → far. There is no in-domain / out-of-domain boolean.
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
                          {map.data.manifest.source_sha256.slice(0, 12)}; built{" "}
                          {map.data.manifest.built_at ?? "unknown"}.
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
    </div>
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
    const fmt = n.endsWith(".json") ? "json" : n.endsWith(".csv") || n.endsWith(".tsv") ? "csv" : null;
    if (!fmt) {
      setUploadError(new Error("Please upload a .json or .csv file."));
      return;
    }
    try {
      const r: ParseResult = await api.parseSignature({ file: f, format: fmt });
      if (r.aligned && r.signature) onSignature(r.signature);
      else setUploadError(new Error("The file could not be aligned to the model schema."));
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
        All demo signatures use the shared 978-gene schema, so they work with every endpoint.
      </p>
      {uploadError != null && <ErrorNotice error={uploadError} />}

      <details className="reference-advanced">
        <summary>Advanced technical input</summary>
        <label className="advanced-field">
          <span>Signature (JSON: gene symbol → value, the 978 landmark genes)</span>
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
