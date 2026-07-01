// Model evidence (detail) — the restructured endpoint detail: variants list (selector when >1),
// metrics with CI, integrated limitations, and the read-only model card. Variant-aware, honest.

import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "../api/client";
import { ErrorNotice } from "../components/ErrorNotice";
import { StatusBadge } from "../components/StatusBadge";
import { VariantView } from "../components/VariantView";
import { useAsync } from "../hooks/useAsync";

export function ModelEvidence() {
  const { id = "" } = useParams();
  const { data, error, loading } = useAsync(() => api.getEndpoint(id), [id]);
  const [active, setActive] = useState(0);

  if (loading) return <p className="text-sm text-muted">Loading endpoint…</p>;
  if (error != null) return <ErrorNotice error={error} />;
  if (!data) return null;

  // variants is a LIST (forward-compat). Show a selector ONLY when more than one exists.
  const variants = data.variants;
  const current = variants[Math.min(active, variants.length - 1)];

  return (
    <div className="space-y-6">
      <Link to="/library" className="text-sm text-muted hover:text-ink">
        ← Model Library
      </Link>

      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-semibold tracking-tight text-ink">{data.biological_target}</h1>
        <StatusBadge status={data.status} />
        <span className="font-mono text-sm text-muted">
          {data.endpoint_id} · v{data.version}
        </span>
      </div>
      <p className="text-sm text-muted">Sources: {data.source_refs.join(", ") || "—"}</p>

      {variants.length > 1 && (
        <div className="flex flex-wrap gap-2" role="tablist" aria-label="Context variants">
          {variants.map((v, i) => (
            <button
              key={v.variant_id}
              role="tab"
              aria-selected={i === active}
              onClick={() => setActive(i)}
              className={`rounded-md border px-3 py-1.5 text-sm ${
                i === active ? "border-brand bg-brand text-white" : "border-line bg-white text-ink"
              }`}
            >
              {v.variant_id}
            </button>
          ))}
        </div>
      )}

      {current && <VariantView variant={current} />}
    </div>
  );
}
