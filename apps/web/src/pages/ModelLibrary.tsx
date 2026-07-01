// Model Library — the endpoint catalogue + evidence, rendered from /endpoints (never hardcoded).
// The detailed evidence/limitations live here (not on the landing page); Analyze is the primary flow.

import { api } from "../api/client";
import { EndpointCard } from "../components/EndpointCard";
import { ErrorNotice } from "../components/ErrorNotice";
import { useAsync } from "../hooks/useAsync";

export function ModelLibrary() {
  const { data, error, loading } = useAsync(() => api.listEndpoints(), []);

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold tracking-tight text-ink">Model Library</h1>
        <p className="text-sm text-muted">
          Registered endpoints and their evidence — rendered from the API. All current endpoints are
          experimental.
        </p>
      </header>

      {loading && <p className="text-sm text-muted">Loading endpoints…</p>}
      {error != null && <ErrorNotice error={error} />}
      {data && (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.map((e) => (
            <EndpointCard key={e.endpoint_id} endpoint={e} />
          ))}
        </div>
      )}
    </div>
  );
}
