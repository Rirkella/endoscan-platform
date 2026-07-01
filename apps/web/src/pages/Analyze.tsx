// Analyze — the primary, default flow. Bring a transcriptomic signature (demo picker / JSON
// paste; upload is a disabled Phase-2 placeholder), run it across whatever /endpoints returns
// (client-side fan-out), and explore per-endpoint signal cards + a count-adaptive comparison.
// Nothing about the endpoint set is hardcoded.

import { useState } from "react";

import { api } from "../api/client";
import type { Signature } from "../api/types";
import { ComparisonViz } from "../components/ComparisonViz";
import { ErrorNotice } from "../components/ErrorNotice";
import { ScoreCardGrid } from "../components/ScoreCardGrid";
import { SignatureInput } from "../components/SignatureInput";
import { useAnalyze } from "../hooks/useAnalyze";
import { useAsync } from "../hooks/useAsync";

export function Analyze() {
  const endpoints = useAsync(() => api.listEndpoints(), []);
  const analyze = useAnalyze(endpoints.data ?? []);
  const [signature, setSignature] = useState<Signature | null>(null);

  const ready = (endpoints.data?.length ?? 0) > 0 && signature != null;

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight text-ink">Analyze a signature</h1>
        <p className="mt-1 max-w-2xl text-sm text-muted">
          Bring a transcriptomic signature and see which endpoint activity signals it is consistent
          with, under EndoScan&rsquo;s experimental models. Results carry their limitations.
        </p>
      </header>

      <div className="grid grid-cols-1 gap-8 lg:grid-cols-[minmax(0,22rem)_1fr]">
        {/* Input column */}
        <div className="space-y-3">
          {endpoints.error != null && <ErrorNotice error={endpoints.error} />}
          <SignatureInput onSignature={setSignature} disabled={analyze.running} />
          {signature && (
            <div className="rounded-md border border-line bg-surface px-3 py-2">
              <p className="text-xs text-muted">
                Signature loaded ({Object.keys(signature).length} genes). The API validates the gene
                set on analyze.
              </p>
              <button
                type="button"
                className="mt-2 w-full rounded-md bg-brand px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
                disabled={!ready || analyze.running}
                onClick={() => signature && analyze.run(signature)}
              >
                {analyze.running ? "Analyzing…" : "Analyze across endpoints"}
              </button>
            </div>
          )}
        </div>

        {/* Results column */}
        <div className="space-y-6">
          {analyze.signals.length === 0 && !analyze.running && (
            <p className="text-sm text-muted">
              Load a signature and run Analyze — one signal card per registered endpoint will appear
              here.
            </p>
          )}
          {analyze.signature && analyze.signals.length > 0 && (
            <>
              <ComparisonViz signals={analyze.signals} />
              <ScoreCardGrid signals={analyze.signals} signature={analyze.signature} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
