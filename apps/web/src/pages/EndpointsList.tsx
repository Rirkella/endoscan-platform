import { api } from "../api/client";
import { EndpointCard } from "../components/EndpointCard";
import { ErrorNotice } from "../components/ErrorNotice";
import { useAsync } from "../hooks/useAsync";

export function EndpointsList() {
  const { data, error, loading } = useAsync(() => api.listEndpoints(), []);

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">Registered endpoints</h1>
        <p className="text-sm text-muted">
          Rendered from the API — status shown up front. All current endpoints are experimental.
        </p>
      </div>

      {loading && <p className="text-sm text-muted">Loading endpoints…</p>}
      {error != null && <ErrorNotice error={error} />}
      {data && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {data.map((e) => (
            <EndpointCard key={e.endpoint_id} endpoint={e} />
          ))}
        </div>
      )}
    </div>
  );
}
