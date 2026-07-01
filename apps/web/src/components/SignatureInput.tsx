// Signature input: (a) a picker of bundled REAL demo signatures (labeled), and (b) JSON paste.
// Client-side we only check the shape is an object of gene->number; the API is the source of
// truth for gene-set validation (its 422 message is surfaced verbatim on the results panel).
// No random generation, ever.

import { useState } from "react";

import type { Signature } from "../api/types";
import { type DemoSignature, demoSignatures } from "../demo-signatures";

interface Props {
  onSignature: (sig: Signature) => void;
  disabled?: boolean;
}

function parseSignature(text: string): Signature {
  const parsed = JSON.parse(text) as unknown;
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Signature must be a JSON object of gene symbol → number.");
  }
  const entries = Object.entries(parsed as Record<string, unknown>);
  if (entries.length === 0) throw new Error("Signature is empty.");
  const out: Signature = {};
  for (const [gene, value] of entries) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      throw new Error(`Gene "${gene}" must map to a finite number.`);
    }
    out[gene] = value;
  }
  return out;
}

export function SignatureInput({ onSignature, disabled }: Props) {
  const [text, setText] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const [demoId, setDemoId] = useState("");
  const selectedDemo = demoSignatures.find((d) => d.id === demoId);

  function pickDemo(id: string) {
    setDemoId(id);
    const demo = demoSignatures.find((d) => d.id === id);
    if (demo) {
      setText(JSON.stringify(demo.signature, null, 0));
      setParseError(null);
    }
  }

  function submit() {
    try {
      const sig = parseSignature(text);
      setParseError(null);
      onSignature(sig);
    } catch (e) {
      setParseError(e instanceof Error ? e.message : "Invalid JSON.");
    }
  }

  return (
    <div className="space-y-3">
      {demoSignatures.length > 0 ? (
        <div>
          <label className="block text-sm font-medium text-ink mb-1" htmlFor="demo-select">
            Demo example — real curated signature, illustrative only
          </label>
          <select
            id="demo-select"
            className="w-full rounded-md border border-line bg-white px-3 py-2 text-sm"
            value={demoId}
            disabled={disabled}
            onChange={(e) => pickDemo(e.target.value)}
          >
            <option value="">Select a bundled demo signature…</option>
            {demoSignatures.map((d: DemoSignature) => (
              <option key={d.id} value={d.id}>
                {d.label}
              </option>
            ))}
          </select>
          {selectedDemo && (
            <p className="mt-1 text-xs text-muted">
              Real curated signature, illustrative only — {selectedDemo.provenance}
            </p>
          )}
        </div>
      ) : (
        <p className="text-xs text-muted rounded-md border border-line bg-surface px-3 py-2">
          No demo signatures are bundled yet (real curated signatures are added by the operator).
          Paste a signature as JSON below.
        </p>
      )}

      <div>
        <label className="block text-sm font-medium text-ink mb-1" htmlFor="sig-json">
          Signature (JSON: gene symbol → value, the 978 landmark genes)
        </label>
        <textarea
          id="sig-json"
          className="w-full h-40 rounded-md border border-line bg-white px-3 py-2 font-mono text-xs"
          placeholder='{ "A1BG": 0.12, "A1CF": -0.4, "…": 0.0 }'
          value={text}
          disabled={disabled}
          onChange={(e) => {
            setText(e.target.value);
            setDemoId("");
          }}
        />
      </div>

      {parseError && (
        <p role="alert" className="text-sm text-red-700">
          {parseError}
        </p>
      )}

      <button
        type="button"
        className="rounded-md bg-brand px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
        disabled={disabled || text.trim().length === 0}
        onClick={submit}
      >
        Use this signature
      </button>
    </div>
  );
}
