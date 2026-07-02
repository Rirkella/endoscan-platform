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
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Explore the data space</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          A 2-D map of the transcriptomic signatures each endpoint model was trained on. This is a{" "}
          <strong>visualization of the training data&rsquo;s structure</strong> — not a model
          boundary and not proof of anything.
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
            <p className="text-sm text-muted">Loading the data-space map…</p>
          )}

          {notComputed && context != null && (
            <div className="rounded-lg border border-line bg-surface p-6 text-sm text-muted">
              <p className="font-medium text-ink">Data-space map not yet computed</p>
              <p className="mt-1">
                No UMAP artifact has been built for <strong>{context}</strong> yet. This view stays
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
                UMAP is a visualization of how the training signatures relate — it is{" "}
                <strong>not a model boundary and not proof</strong>. Distances used for placement
                are computed in the full 978-gene space, not on this 2-D map.
              </p>
            </>
          )}
        </div>

        {/* Placement column */}
        <div className="space-y-3">
          <div className="rounded-md border border-line bg-surface p-3">
            <h2 className="text-sm font-medium text-ink">Place a signature</h2>
            <p className="mt-1 text-xs text-muted">
              Bring a signature to see its nearest known compounds. Placement is{" "}
              <strong>approximate</strong> (nearest neighbours) — not an exact projection.
            </p>
            <div className="mt-3">
              <SignatureInput onSignature={onSignature} disabled={explore.running || !context} />
            </div>
          </div>

          {explore.running && <p className="text-sm text-muted">Finding nearest neighbours…</p>}
          {explore.error != null && <ErrorNotice error={explore.error} />}

          {explore.result && (
            <div className="space-y-2 rounded-md border border-line bg-white p-3">
              <p className="text-sm font-medium text-ink">
                Approximate placement (nearest neighbors)
              </p>
              <p className="text-xs text-muted">
                Nearest known compound:{" "}
                <span className="font-mono text-ink">
                  {explore.result.neighbors[0]?.compound_id}
                </span>{" "}
                at distance {explore.result.neighbors[0]?.distance.toFixed(2)}.
              </p>
              <p className="text-xs text-muted">
                Distance to the {explore.result.domain.k}-th nearest training compound:{" "}
                <strong>{explore.result.domain.query_kth_distance.toFixed(2)}</strong>. Typical
                training range (p25–p95):{" "}
                {explore.result.domain.training_reference_quantiles.p25?.toFixed(2)}–
                {explore.result.domain.training_reference_quantiles.p95?.toFixed(2)}. This
                signature&rsquo;s isolation is at the{" "}
                {Math.round(explore.result.domain.percentile * 100)}th percentile of the training
                set.
              </p>
              <p className="text-[11px] text-muted">
                These are computed distances, shown for your judgement — EndoScan makes{" "}
                <strong>no domain-membership verdict</strong> (neither inside nor outside).
              </p>
              <ul className="mt-1 space-y-0.5 text-xs">
                {explore.result.neighbors.map((n) => (
                  <li key={n.compound_id} className="flex justify-between gap-2">
                    <span className="font-mono text-ink">{n.compound_id}</span>
                    <span className="text-muted">
                      {n.distance.toFixed(2)}
                      {n.label ? ` · ${n.label}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
