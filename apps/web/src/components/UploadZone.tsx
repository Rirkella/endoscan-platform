// Signature file upload. The client does FILE-SHAPE sanity only (extension -> format, non-empty)
// and posts to /signatures/parse; the SERVER is the one gene validator. The preview reflects the
// real parse result; a 4xx shows the API's verbatim message. Nothing is persisted client-side.

import { useState } from "react";

import { api } from "../api/client";
import type { ParseResult, Signature } from "../api/types";
import { ErrorNotice } from "./ErrorNotice";

interface Props {
  onSignature: (sig: Signature) => void;
  disabled?: boolean;
}

type InputFormat = "json" | "csv" | "tsv";

function formatOf(name: string): InputFormat | null {
  const n = name.toLowerCase();
  if (n.endsWith(".json")) return "json";
  if (n.endsWith(".csv")) return "csv";
  if (n.endsWith(".tsv")) return "tsv";
  return null;
}

export function UploadZone({ onSignature, disabled }: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<InputFormat | null>(null);
  const [parsing, setParsing] = useState(false);
  const [result, setResult] = useState<ParseResult | null>(null);
  const [error, setError] = useState<unknown>(null);

  async function doParse(f: File, fmt: InputFormat, opts: { sample?: string } = {}) {
    setParsing(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.parseSignature({ file: f, format: fmt, ...opts });
      setResult(r);
    } catch (e) {
      setError(e);
    } finally {
      setParsing(false);
    }
  }

  function onPick(f: File | null) {
    setResult(null);
    setError(null);
    setFile(f);
    if (!f) return;
    const fmt = formatOf(f.name);
    setFormat(fmt);
    if (!fmt) {
      setError(new Error("Please upload a .json, .csv or .tsv file."));
      return;
    }
    void doParse(f, fmt);
  }

  return (
    <div className="space-y-2">
      <label
        className={`block rounded-md border-2 border-dashed px-4 py-5 text-center ${
          disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer hover:border-brand"
        } border-line bg-surface`}
      >
        <input
          type="file"
          accept=".json,.csv,.tsv"
          className="sr-only"
          disabled={disabled}
          onChange={(e) => onPick(e.target.files?.[0] ?? null)}
        />
        <span className="text-sm font-medium text-ink">
          {file ? file.name : "Upload a signature file (CSV / JSON)"}
        </span>
        <span className="mt-1 block text-xs text-muted">
          The server validates the gene set independently against every endpoint schema.
        </span>
      </label>

      {parsing && <p className="text-sm text-muted">Parsing…</p>}

      {error != null && <ErrorNotice error={error} />}
      {result?.preview.needs_sample && file && format && (
        <div className="rounded-md border border-line bg-white p-3 text-sm">
          <p className="text-ink">
            {result.preview.samples?.length} samples found — pick one:
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            {result.preview.samples?.map((s) => (
              <button
                key={s}
                type="button"
                className="rounded-md border border-line px-2 py-1 text-xs hover:border-brand"
                onClick={() => void doParse(file, format, { sample: s })}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {result?.ready && result.signature && (
        <div className="rounded-md border border-line bg-white p-3">
          <p className="text-sm text-ink">
            Parsed {result.preview.n_detected} genes; {result.compatible_endpoint_ids.length} of{" "}
            {result.compatibility.length} endpoint models are compatible.
          </p>
          <button
            type="button"
            className="mt-2 rounded-md bg-brand px-4 py-2 text-sm font-medium text-white"
            disabled={result.compatible_endpoint_ids.length === 0}
            onClick={() => result.signature && onSignature(result.signature)}
          >
            Use this signature
          </button>
        </div>
      )}
    </div>
  );
}
