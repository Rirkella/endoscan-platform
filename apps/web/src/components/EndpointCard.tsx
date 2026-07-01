import { Link } from "react-router-dom";

import type { EndpointSummary } from "../api/types";
import { StatusBadge } from "./StatusBadge";

export function EndpointCard({ endpoint }: { endpoint: EndpointSummary }) {
  return (
    <Link
      to={`/endpoints/${encodeURIComponent(endpoint.endpoint_id)}`}
      className="block rounded-lg border border-line bg-white p-4 hover:border-brand transition-colors"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="font-semibold text-ink">{endpoint.biological_target}</span>
        <StatusBadge status={endpoint.status} />
      </div>
      <div className="mt-1 text-sm text-muted">
        <span className="font-mono">{endpoint.endpoint_id}</span> · input:{" "}
        {endpoint.input_type}
      </div>
    </Link>
  );
}
