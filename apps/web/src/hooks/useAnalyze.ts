import { useState } from "react";

import { api } from "../api/client";
import type { EndpointSummary, PredictionResult, Signature } from "../api/types";

// One endpoint's prediction outcome. `result` on success, `error` on failure — a single
// endpoint failing does NOT sink the others (each /predict call is independent).
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

// Client-side fan-out of /predict across whatever /endpoints returns (Phase 1; a single
// analyze-all endpoint is Phase 2). Never hardcodes the endpoint set.
export function useAnalyze(endpoints: EndpointSummary[]) {
  const [state, setState] = useState<AnalyzeState>({
    signals: [],
    signature: null,
    running: false,
  });

  async function run(signature: Signature) {
    setState({ signals: [], signature, running: true });
    const signals = await Promise.all(
      endpoints.map(async (e): Promise<EndpointSignal> => {
        try {
          const result = await api.predict(e.endpoint_id, signature);
          return { endpoint_id: e.endpoint_id, biological_target: e.biological_target, result, error: null };
        } catch (error) {
          return { endpoint_id: e.endpoint_id, biological_target: e.biological_target, result: null, error };
        }
      }),
    );
    setState({ signals, signature, running: false });
  }

  return { ...state, run };
}
