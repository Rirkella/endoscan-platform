import { useMemo, useState } from "react";

import { api } from "../api/client";
import { ErrorNotice } from "../components/ErrorNotice";
import { ExplanationResultView } from "../components/ExplanationResultView";
import { PredictionResultView } from "../components/PredictionResultView";
import { SignatureInput } from "../components/SignatureInput";
import type { ExplanationResult, PredictionResult, Signature } from "../api/types";
import { useAsync } from "../hooks/useAsync";

type Result =
  | { kind: "predict"; data: PredictionResult }
  | { kind: "explain"; data: ExplanationResult };

export function Analyze() {
  const endpoints = useAsync(() => api.listEndpoints(), []);
  const [endpointId, setEndpointId] = useState("");
  const [signature, setSignature] = useState<Signature | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const selectedId = useMemo(() => {
    if (endpointId) return endpointId;
    return endpoints.data?.[0]?.endpoint_id ?? "";
  }, [endpointId, endpoints.data]);

  const target =
    endpoints.data?.find((e) => e.endpoint_id === selectedId)?.biological_target ?? selectedId;

  async function run(kind: "predict" | "explain") {
    if (!signature || !selectedId) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      if (kind === "predict") {
        setResult({ kind, data: await api.predict(selectedId, signature) });
      } else {
        setResult({ kind, data: await api.explain(selectedId, signature) });
      }
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">Analyze a signature</h1>
        <p className="text-sm text-muted">
          Pick a real demo signature or paste one as JSON, then predict or explain. Results are
          experimental and carry their limitations.
        </p>
      </div>

      {endpoints.error != null && <ErrorNotice error={endpoints.error} />}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-ink mb-1" htmlFor="endpoint-select">
              Endpoint
            </label>
            <select
              id="endpoint-select"
              className="w-full rounded-md border border-line bg-white px-3 py-2 text-sm"
              value={selectedId}
              onChange={(e) => setEndpointId(e.target.value)}
            >
              {(endpoints.data ?? []).map((e) => (
                <option key={e.endpoint_id} value={e.endpoint_id}>
                  {e.biological_target} ({e.endpoint_id}) — {e.status}
                </option>
              ))}
            </select>
          </div>

          <SignatureInput onSignature={setSignature} disabled={busy} />

          {signature && (
            <div className="flex gap-3">
              <button
                type="button"
                className="rounded-md bg-brand px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
                disabled={busy}
                onClick={() => run("predict")}
              >
                Predict
              </button>
              <button
                type="button"
                className="rounded-md border border-line bg-white px-4 py-2 text-sm font-medium text-ink disabled:opacity-50"
                disabled={busy}
                onClick={() => run("explain")}
              >
                Explain
              </button>
            </div>
          )}
          {signature && (
            <p className="text-xs text-muted">
              Signature loaded ({Object.keys(signature).length} genes). The API validates the gene
              set.
            </p>
          )}
        </div>

        <div className="space-y-4">
          {busy && <p className="text-sm text-muted">Running…</p>}
          {error != null && <ErrorNotice error={error} />}
          {result?.kind === "predict" && (
            <PredictionResultView result={result.data} target={target} />
          )}
          {result?.kind === "explain" && <ExplanationResultView result={result.data} />}
          {!busy && !error && !result && (
            <p className="text-sm text-muted">Results will appear here.</p>
          )}
        </div>
      </div>
    </div>
  );
}
