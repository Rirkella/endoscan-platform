// Responsive grid of endpoint signal cards — ONE per API result. Auto-wraps for 2 / 3 / 4 / 6+
// endpoints (no fixed 2-or-3 layout); the card set is whatever the API returned, never hardcoded.

import type { Signature } from "../api/types";
import type { EndpointSignal } from "../hooks/useAnalyze";
import { EndpointSignalCard } from "./EndpointSignalCard";

export function ScoreCardGrid({
  signals,
  signature,
}: {
  signals: EndpointSignal[];
  signature: Signature;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {signals.map((s) => (
        <EndpointSignalCard key={s.endpoint_id} signal={s} signature={signature} />
      ))}
    </div>
  );
}
