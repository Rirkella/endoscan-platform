// Explore — the data-space (UMAP) view over an endpoint's REAL training signatures. This screen
// is honesty-first by construction:
//   1. UMAP is a VISUALIZATION of how training signatures relate — not a model boundary, not proof.
//   2. A submitted signature is placed APPROXIMATELY (centroid of nearest neighbours found in the
//      full 978-gene space) — never an exact 2-D projection.
//   3. There is NO in-/out-of-domain verdict — only a DEFINED distance metric vs the training set.
// When no map is committed for a context, we show a calm "not yet computed" state (no fake points).

import { useEffect, useState } from "react";

import { EndoscanApiError, api } from "../api/client";
import type { Signature } from "../api/types";
import { ErrorNotice } from "../components/ErrorNotice";
import { ExploreScatter } from "../components/ExploreScatter";
import { SignatureInput } from "../components/SignatureInput";
import { useAsync } from "../hooks/useAsync";
import { useExplore } from "../hooks/useExplore";

// Transparent percentile -> plain similarity band. `percentile` is the fraction of training
// compounds whose own k-th-neighbour distance is <= the query's, so a HIGHER percentile means the
// signature is MORE isolated (further) from the reference data. The cut is fixed and shown in the
// Technical details, so "close/moderately close/far" is always traceable to the real number.
type SimilarityBand = "close" | "moderately close" | "far";
function similarityBand(percentile: number): SimilarityBand {
  if (percentile <= 0.5) return "close";
  if (percentile <= 0.9) return "moderately close";
  return "far";
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1 text-xs text-muted">
      <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ backgroundColor: color }} />
      {label}
    </span>
  );
}

export function Explore() {
  const endpoints = useAsync(() => api.listEndpoints(), []);
  const [context, setContext] = useState<string | null>(null);

  // Default the context to the first registered endpoint once endpoints load.
  useEffect(() => {
    if (context == null && (endpoints.data?.length ?? 0) > 0) {
      setContext(endpoints.data![0].endpoint_id);
    }
  }, [endpoints.data, context]);

  const map = useAsync(() => (context ? api.exploreUmap(context) : Promise.reject(new Error("no context"))), [context]);
  const explore = useExplore(context);

  const notComputed = map.error instanceof EndoscanApiError && map.error.status === 404;

  function onSignature(sig: Signature) {
    explore.locate(sig);
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Reference landscape</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          See where your signature sits among known reference signatures. Nearby compounds show
          similar expression patterns — this can suggest comparisons worth investigating, but it
          does not prove the same biological effect.
        </p>
      </header>

      {/* Context selector — one map per endpoint (its own compounds + labels). */}
      <div className="flex items-center gap-2">
        <label className="text-sm text-muted" htmlFor="explore-context">
          Endpoint
        </label>
        <select
          id="explore-context"
          className="rounded-md border border-line bg-white px-2 py-1 text-sm"
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
      </div>

      {endpoints.error != null && <ErrorNotice error={endpoints.error} />}

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-[1fr_minmax(0,20rem)]">
        {/* Map column */}
        <div className="space-y-3">
          {(map.loading || context == null) && (
            <p className="text-sm text-muted">Loading the reference landscape…</p>
          )}

          {notComputed && context != null && (
            <div className="rounded-lg border border-line bg-surface p-6 text-sm text-muted">
              <p className="font-medium text-ink">Reference landscape not yet available</p>
              <p className="mt-1">
                No reference map has been built for <strong>{context}</strong> yet. This view stays
                empty until a real map is computed on the server — no placeholder points are shown.
              </p>
            </div>
          )}

          {map.error != null && !notComputed && context != null && (
            <ErrorNotice error={map.error} />
          )}

          {map.data && (
            <>
              <ExploreScatter points={map.data.points} locate={explore.result} />
              <div className="flex flex-wrap items-center gap-4">
                <p className="text-xs text-muted">
                  {map.data.counts.n_total} training compounds
                  {map.data.manifest.label_status !== "none" &&
                  map.data.manifest.label_status !== "unjoined" ? (
                    <>
                      {" "}
                      ({map.data.counts.n_active} active / {map.data.counts.n_inactive} inactive
                      {map.data.counts.n_unlabeled > 0
                        ? ` / ${map.data.counts.n_unlabeled} unlabeled`
                        : ""}
                      )
                    </>
                  ) : (
                    <> (uncoloured — no reliable labels joined)</>
                  )}
                </p>
                {map.data.counts.n_active > 0 && <LegendDot color="#b45309" label="active" />}
                {map.data.counts.n_inactive > 0 && <LegendDot color="#475569" label="inactive" />}
              </div>
              <p className="text-xs text-muted">
                This map shows how known reference signatures relate to each other. It&rsquo;s a
                visual guide — <strong>not a model boundary, and not proof of anything</strong>.
              </p>
            </>
          )}
        </div>

        {/* Placement column */}
        <div className="space-y-3">
          <div className="rounded-md border border-line bg-surface p-3">
            <h2 className="text-sm font-medium text-ink">Place a signature</h2>
            <p className="mt-1 text-xs text-muted">
              Bring a signature to find the most similar known compounds. Its position is{" "}
              <strong>approximate</strong> — based on the most similar signatures, not an exact
              placement.
            </p>
            <div className="mt-3">
              <SignatureInput onSignature={onSignature} disabled={explore.running || !context} />
            </div>
          </div>

          {explore.running && <p className="text-sm text-muted">Finding similar compounds…</p>}
          {explore.error != null && <ErrorNotice error={explore.error} />}

          {explore.result && (
            <div className="space-y-3 rounded-md border border-line bg-white p-3">
              <p className="text-sm font-medium text-ink">
                Approximate position based on the most similar known signatures — not an exact
                placement of your signature.
              </p>

              {/* Plain, graded similarity readout — mapped transparently from the real percentile
                  (the number + the band cut live in Technical details below). */}
              <div>
                <p className="text-xs font-medium text-ink">
                  How similar is this signature to known data?
                </p>
                <p className="mt-0.5 text-xs text-muted" data-testid="similarity-readout">
                  This signature is{" "}
                  <strong>{similarityBand(explore.result.domain.percentile)}</strong> to
                  EndoScan&rsquo;s reference data.
                </p>
                <p className="mt-1 text-[11px] text-muted">
                  EndoScan makes <strong>no domain-membership verdict</strong> (neither inside nor
                  outside) — this is a graded comparison, not a pass/fail.
                </p>
              </div>

              <div>
                <p className="text-xs font-medium text-ink">
                  Compounds with similar expression patterns
                </p>
                <p className="mt-0.5 text-[11px] text-muted">
                  These may be useful comparisons. Similar patterns do not prove the same effect.
                </p>
                <ul className="mt-1 space-y-0.5 text-xs" data-testid="similar-compounds">
                  {explore.result.neighbors.map((n) => (
                    <li key={n.compound_id} className="flex justify-between gap-2">
                      <span className="font-mono text-ink">{n.compound_id}</span>
                      {n.label && <span className="text-muted">{n.label}</span>}
                    </li>
                  ))}
                </ul>
              </div>

              {/* Technical details — one level deeper. Nothing is removed: every technical field
                  the API returns lives here, so the plain "close/far" above is fully traceable. */}
              <details data-testid="explore-technical" className="rounded border border-line bg-surface p-2">
                <summary className="cursor-pointer text-xs font-medium text-ink">
                  Technical details
                </summary>
                <dl className="mt-2 space-y-1 text-[11px] text-muted">
                  <div>
                    <dt className="inline font-medium">Similarity metric:</dt>{" "}
                    <dd className="inline">
                      distance to the {explore.result.domain.k}-th nearest reference signature ={" "}
                      {explore.result.domain.query_kth_distance.toFixed(3)} (
                      {explore.result.domain.metric}); typical training range p25–p95:{" "}
                      {explore.result.domain.training_reference_quantiles.p25?.toFixed(3)}–
                      {explore.result.domain.training_reference_quantiles.p95?.toFixed(3)}.
                    </dd>
                  </div>
                  <div>
                    <dt className="inline font-medium">Percentile → band:</dt>{" "}
                    <dd className="inline">
                      percentile ={" "}
                      <strong>{(explore.result.domain.percentile * 100).toFixed(1)}%</strong> of
                      training compounds are at least this isolated. Bands: ≤50% → close, 50–90% →
                      moderately close, &gt;90% → far. There is no in-domain / out-of-domain
                      boolean.
                    </dd>
                  </div>
                  <div>
                    <dt className="inline font-medium">Feature space:</dt>{" "}
                    <dd className="inline">
                      landmark-gene expression space (the endpoint&rsquo;s feature schema); k ={" "}
                      {explore.result.domain.k}.
                    </dd>
                  </div>
                  {map.data && (
                    <>
                      <div>
                        <dt className="inline font-medium">Projection:</dt>{" "}
                        <dd className="inline">
                          UMAP (n_neighbors={String(map.data.manifest.umap.n_neighbors)}, min_dist=
                          {String(map.data.manifest.umap.min_dist)}, metric=
                          {String(map.data.manifest.umap.metric)}, random_state=
                          {String(map.data.manifest.umap.random_state)}, v
                          {String(map.data.manifest.umap.umap_version)}). Visualization only.
                        </dd>
                      </div>
                      <div>
                        <dt className="inline font-medium">Reference dataset:</dt>{" "}
                        <dd className="inline">
                          {map.data.manifest.n_compounds} compounds ({context}); labels:{" "}
                          {map.data.manifest.label_status}.
                        </dd>
                      </div>
                      <div>
                        <dt className="inline font-medium">Version:</dt>{" "}
                        <dd className="inline">
                          source {map.data.manifest.source_sha256.slice(0, 12)}; built{" "}
                          {map.data.manifest.built_at ?? "unknown"}.
                        </dd>
                      </div>
                    </>
                  )}
                </dl>
              </details>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
