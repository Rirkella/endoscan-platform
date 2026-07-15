import { useState } from "react";

import { EndoscanApiError, api } from "../api/client";
import type { AnalyzeResponse, EndpointSummary, PredictionResult, Signature } from "../api/types";

// One endpoint's prediction outcome. `result` on success, `error` on failure — a single
// endpoint failing does NOT sink the others.
export interface EndpointSignal {
  endpoint_id: string;
  biological_target: string;
  model_version?: string;
  source_refs?: string[];
  result: PredictionResult | null;
  error: unknown;
}

export interface AnalyzeState {
  signals: EndpointSignal[];
  signature: Signature | null;
  running: boolean;
  summary: AnalyzeResponse["summary"] | null;
}

// Phase 2: one POST /analyze call returns the per-endpoint array (server-side fan-out with
// per-endpoint isolation). Falls back to a client-side /predict fan-out if /analyze is absent
// (404) — graceful during rollout. Never hardcodes the endpoint set.
export function useAnalyze(endpoints: EndpointSummary[]) {
  const [state, setState] = useState<AnalyzeState>({
    signals: [],
    signature: null,
    running: false,
    summary: null,
  });

  async function fanOut(signature: Signature, endpointIds: string[]): Promise<EndpointSignal[]> {
    return Promise.all(
      endpoints.filter((e) => endpointIds.includes(e.endpoint_id)).map(async (e): Promise<EndpointSignal> => {
        try {
          const result = await api.predict(e.endpoint_id, signature);
          return {
            endpoint_id: e.endpoint_id,
            biological_target: e.biological_target,
            result,
            error: null,
          };
        } catch (error) {
          return { endpoint_id: e.endpoint_id, biological_target: e.biological_target, result: null, error };
        }
      }),
    );
  }

  async function run(signature: Signature, endpointIds: string[], allowExtra = false) {
    setState({ signals: [], signature, running: true, summary: null });
    let signals: EndpointSignal[];
    let summary: AnalyzeResponse["summary"] | null = null;
    try {
      const resp = await api.analyze(signature, endpointIds, allowExtra);
      summary = resp.summary;
      signals = resp.results.map((r) => ({
        endpoint_id: r.endpoint_id,
        biological_target: r.biological_target,
        model_version: r.model_version,
        source_refs: r.source_refs,
        result: r.result,
        // Rehydrate the per-endpoint API error so ErrorNotice renders the verbatim message.
        error: r.error
          ? new EndoscanApiError({
              status: 422,
              error: r.error.error,
              detail: r.error.detail,
              endpoint_id: r.error.endpoint_id,
              request_id: r.error.request_id,
            })
          : null,
      }));
    } catch (err) {
      if (err instanceof EndoscanApiError && err.status === 404) {
        signals = await fanOut(signature, endpointIds); // fallback: /analyze not deployed yet
        const succeeded = signals.filter((signal) => signal.result != null).length;
        summary = {
          requested: signals.length,
          succeeded,
          failed: signals.length - succeeded,
          status: succeeded === 0 ? "all_failed" : succeeded === signals.length ? "ok" : "partial",
        };
      } else {
        throw err;
      }
    }
    setState({ signals, signature, running: false, summary });
  }

  return { ...state, run };
}
