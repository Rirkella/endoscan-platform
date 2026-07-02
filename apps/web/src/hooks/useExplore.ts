import { useState } from "react";

import { EndoscanApiError, api } from "../api/client";
import type { ExploreLocateResult, Signature } from "../api/types";

// Placement state for a submitted signature. `notComputed` is set when the map exists but a
// 404 came back for locate (belt-and-braces); errors are surfaced verbatim otherwise.
export interface LocateState {
  result: ExploreLocateResult | null;
  running: boolean;
  error: unknown;
}

// The map itself is loaded by the page via useAsync(api.exploreUmap). This hook owns ONLY the
// on-demand "place my signature" call so the scatter can highlight the nearest neighbours.
export function useExplore(context: string | null) {
  const [state, setState] = useState<LocateState>({ result: null, running: false, error: null });

  async function locate(signature: Signature) {
    if (!context) return;
    setState({ result: null, running: true, error: null });
    try {
      const result = await api.exploreLocate(context, signature);
      setState({ result, running: false, error: null });
    } catch (error) {
      // A 404 here means the map isn't computed for this context — a calm empty state.
      const notComputed = error instanceof EndoscanApiError && error.status === 404;
      setState({ result: null, running: false, error: notComputed ? null : error });
    }
  }

  function reset() {
    setState({ result: null, running: false, error: null });
  }

  return { ...state, locate, reset };
}
