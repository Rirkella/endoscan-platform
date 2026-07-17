// Model library — the endpoint catalogue, ported to the prototype-v2 grid look and rendered from
// the real /endpoints response (never hardcoded ER/AR). Per-endpoint quantitative evidence (metrics
// with CIs, model card, limitations) lives on the evidence detail (/library/:id) and is NOT
// fabricated here; each card links there. The status chip is driven by the API's status string.

import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { EndpointSummary } from "../api/types";
import { ErrorNotice } from "../components/ErrorNotice";
import { StatusBadge } from "../components/StatusBadge";
import { useAsync } from "../hooks/useAsync";

export function ModelLibrary() {
  const { data, error, loading } = useAsync(() => api.listEndpoints(), []);
  const endpoints: EndpointSummary[] = data ?? [];
  const nExperimental = endpoints.filter((e) => e.status === "experimental").length;

  return (
    <div>
      <div className="page-header">
        <div>
          <p className="eyebrow">Registered endpoints</p>
          <h1>Model library</h1>
          <p className="page-copy">
            Understand where each model can be used, how it was evaluated and what its result does
            not mean. All current endpoints are experimental.
          </p>
        </div>
      </div>

      {loading && <p className="check-plain">Loading endpoints…</p>}
      {error != null && <ErrorNotice error={error} />}

      {data && (
        <>
          <div className="library-summary-band">
            <div>
              <strong>{endpoints.length}</strong>
              <span>registered endpoints</span>
            </div>
            <div>
              <strong>{nExperimental}</strong>
              <span>experimental models</span>
            </div>
            <div>
              <strong>978</strong>
              <span>required landmark genes</span>
            </div>
            <p>
              <span className="status-dot status-dot-amber" aria-hidden />
              No model is validated for clinical or regulatory use.
            </p>
          </div>

          <div className="model-grid">
            {endpoints.map((e) => (
              <article className="model-card" key={e.endpoint_id}>
                <div className="model-card-top">
                  <span className="endpoint-code code-generic">
                    {e.endpoint_id}
                  </span>
                  <StatusBadge status={e.status} />
                </div>
                <h2>{e.biological_target}</h2>
                <p>
                  Transcriptomic pattern classifier using the registered landmark-gene input schema.
                </p>
                <dl>
                  <div>
                    <dt>Endpoint</dt>
                    <dd className="mono">{e.endpoint_id}</dd>
                  </div>
                  <div>
                    <dt>Input type</dt>
                    <dd>{e.input_type}</dd>
                  </div>
                  <div>
                    <dt>Status</dt>
                    <dd>{e.status}</dd>
                  </div>
                </dl>
                <Link
                  className="button outline"
                  to={`/library/${encodeURIComponent(e.endpoint_id)}`}
                >
                  Open model evidence
                </Link>
              </article>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
