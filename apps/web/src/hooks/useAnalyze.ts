import { useState } from "react";

import { EndoscanApiError, api } from "../api/client";
import type { EndpointSummary, PredictionResult, Signature } from "../api/types";

// One endpoint's prediction outcome. `result` on success, `error` on failure — a single
// endpoint failing does NOT sink the others.
export interface EndpointSignal {
  endpoint_id: string;
  biological_target: string;
  result: PredictionResult | null;
  error: unknown;
}

export interface AnalyzeState {
  signals: EndpointSignal[];
  signature: Signature | null;
  running: boolean;
}

// Phase 2: one POST /analyze call returns the per-endpoint array (server-side fan-out with
// per-endpoint isolation). Falls back to a client-side /predict fan-out if /analyze is absent
// (404) — graceful during rollout. Never hardcodes the endpoint set.
export function useAnalyze(endpoints: EndpointSummary[]) {
  const [state, setState] = useState<AnalyzeState>({
    signals: [],
    signature: null,
    running: false,
  });

  async function fanOut(signature: Signature): Promise<EndpointSignal[]> {
    return Promise.all(
      endpoints.map(async (e): Promise<EndpointSignal> => {
        try {
          const result = await api.predict(e.endpoint_id, signature);
          return { endpoint_id: e.endpoint_id, biological_target: e.biological_target, result, error: null };
        } catch (error) {
          return { endpoint_id: e.endpoint_id, biological_target: e.biological_target, result: null, error };
        }
      }),
    );
  }

  async function run(signature: Signature) {
    setState({ signals: [], signature, running: true });
    let signals: EndpointSignal[];
    try {
      const resp = await api.analyze(signature);
      signals = resp.results.map((r) => ({
        endpoint_id: r.endpoint_id,
        biological_target: r.biological_target,
        result: r.result,
        // Rehydrate the per-endpoint API error so ErrorNotice renders the verbatim message.
        error: r.error
          ? new EndoscanApiError({
              status: 422,
              error: r.error.error,
              detail: r.error.detail,
              endpoint_id: r.error.endpoint_id,
            })
          : null,
      }));
    } catch (err) {
      if (err instanceof EndoscanApiError && err.status === 404) {
        signals = await fanOut(signature); // fallback: /analyze not deployed yet
      } else {
        throw err;
      }
    }
    setState({ signals, signature, running: false });
  }

  return { ...state, run };
}
