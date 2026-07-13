// Source step (ported from prototype-v2). Two entry modes:
//   "My signature"  — REAL: upload a file (POST /signatures/parse, server is the one gene validator)
//                     with an advanced "paste JSON" fallback and a bundled-demo picker.
//   "Known molecule" — PLANNED/mock lookup. It lists illustrative example signatures but never
//                     produces an analysis: EndoScan will not infer a result from molecule identity
//                     alone, so this tab only nudges the user to upload a measured signature.
// JSON paste is deliberately a secondary "advanced" action, never the primary visual entry point.

import { useState } from "react";

import { EndoscanApiError, api } from "../../api/client";
import type { ParseResult, Signature } from "../../api/types";
import { type DemoSignature, demoSignatures } from "../../demo-signatures";
import { MockBadge, PlannedBadge } from "../PlannedBadge";
import { ErrorNotice } from "../ErrorNotice";
import type { PreparedInput } from "../../pages/Analyze";
import { MOCK_MEASURED_SIGNATURES } from "../../mock/prototypeMock";

type Mode = "upload" | "molecule";

function formatOf(name: string): "json" | "csv" | null {
  const n = name.toLowerCase();
  if (n.endsWith(".json")) return "json";
  if (n.endsWith(".csv") || n.endsWith(".tsv")) return "csv";
  return null;
}

function parsePastedSignature(text: string): Signature {
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

export function SourceStep({
  onPrepared,
  endpointCount,
}: {
  onPrepared: (input: PreparedInput) => void;
  endpointCount: number;
}) {
  const [mode, setMode] = useState<Mode>("upload");

  return (
    <section className="source-layout">
      <div className="source-main">
        <div className="segmented" role="tablist" aria-label="Input source">
          <button
            role="tab"
            aria-selected={mode === "upload"}
            className={mode === "upload" ? "selected" : ""}
            onClick={() => setMode("upload")}
          >
            My signature
          </button>
          <button
            role="tab"
            aria-selected={mode === "molecule"}
            className={mode === "molecule" ? "selected" : ""}
            onClick={() => setMode("molecule")}
          >
            Known molecule
          </button>
        </div>

        {mode === "upload" ? (
          <UploadPanel onPrepared={onPrepared} onTryMolecule={() => setMode("molecule")} />
        ) : (
          <MoleculePanel onUpload={() => setMode("upload")} />
        )}
      </div>

      <aside className="source-aside">
        <p className="aside-label">How input works</p>
        <ol>
          <li>
            <span>1</span>
            <div>
              <strong>Add biological response</strong>
              <p>Upload measured values or paste a signature.</p>
            </div>
          </li>
          <li>
            <span>2</span>
            <div>
              <strong>Verify before analysis</strong>
              <p>Review gene coverage and model compatibility.</p>
            </div>
          </li>
          <li>
            <span>3</span>
            <div>
              <strong>Inspect evidence</strong>
              <p>See endpoint signals, genes, pathways and limitations.</p>
            </div>
          </li>
        </ol>
        <div className="honesty-note">
          <span className="status-dot status-dot-blue" aria-hidden />
          <p>
            <strong>Transcriptomics first</strong>
            {endpointCount > 0
              ? "Molecule identity is never scored directly — a measured signature is required."
              : "Molecule identity is never scored directly."}
          </p>
        </div>
      </aside>
    </section>
  );
}

function UploadPanel({
  onPrepared,
  onTryMolecule,
}: {
  onPrepared: (input: PreparedInput) => void;
  onTryMolecule: () => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<"json" | "csv" | null>(null);
  const [parsing, setParsing] = useState(false);
  const [result, setResult] = useState<ParseResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [allowExtra, setAllowExtra] = useState(false);

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);
  const [demoId, setDemoId] = useState("");

  async function doParse(
    f: File,
    fmt: "json" | "csv",
    opts: { allow_extra?: boolean; sample?: string } = {},
  ) {
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
    setAllowExtra(false);
    setFile(f);
    if (!f) return;
    const fmt = formatOf(f.name);
    setFormat(fmt);
    if (!fmt) {
      setError(new Error("Please upload a .json or .csv file."));
      return;
    }
    void doParse(f, fmt);
  }

  function useUploaded(r: ParseResult) {
    if (!r.signature) return;
    onPrepared({
      title: file?.name ?? "Uploaded signature",
      subtitle: "Uploaded transcriptomic signature",
      kind: "file",
      signature: r.signature,
      parse: r,
    });
  }

  function loadPaste() {
    try {
      const sig = parsePastedSignature(pasteText);
      setPasteError(null);
      onPrepared({
        title: "Pasted signature",
        subtitle: "Signature entered as JSON",
        kind: "paste",
        signature: sig,
        parse: null,
      });
    } catch (e) {
      setPasteError(e instanceof Error ? e.message : "Invalid JSON.");
    }
  }

  function pickDemo(id: string) {
    setDemoId(id);
    const demo = demoSignatures.find((d) => d.id === id);
    if (!demo) return;
    onPrepared({
      title: demo.label,
      subtitle: `Bundled demo signature — ${demo.provenance}`,
      kind: "demo",
      signature: demo.signature,
      parse: null,
    });
  }

  const extrasError = error instanceof EndoscanApiError && /not in the schema/i.test(error.detail);

  return (
    <div className="upload-panel-wrap">
      <label className="upload-panel">
        <input
          type="file"
          accept=".json,.csv,.tsv"
          className="upload-file-input"
          onChange={(e) => onPick(e.target.files?.[0] ?? null)}
        />
        <div className="upload-symbol">CSV</div>
        <h2>Upload a signature file</h2>
        <p>
          CSV or JSON containing landmark gene values. The server validates the gene set against the
          model schema; coverage is reviewed next.
        </p>
        <span className="button primary upload-cta">{file ? file.name : "Choose data file"}</span>
        <span className="upload-hint">Accepted schema: 978 landmark genes</span>
      </label>

      {parsing && <p className="upload-status">Parsing…</p>}
      {error != null && <ErrorNotice error={error} />}

      {extrasError && file && format && (
        <label className="extras-toggle">
          <input
            type="checkbox"
            checked={allowExtra}
            onChange={(e) => {
              setAllowExtra(e.target.checked);
              if (e.target.checked) void doParse(file, format, { allow_extra: true });
            }}
          />
          Ignore extra genes not in the schema and re-check
        </label>
      )}

      {result?.preview.needs_sample && file && format && (
        <div className="sample-picker">
          <p>{result.preview.samples?.length} samples found — pick one:</p>
          <div className="sample-buttons">
            {result.preview.samples?.map((s) => (
              <button key={s} onClick={() => void doParse(file, format, { sample: s })}>
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {result?.aligned && result.signature && (
        <div className="coverage-summary">
          <div className="coverage-summary-top">
            <div>
              <strong className="tabular">
                {result.preview.n_matched} / {result.n_schema_genes}
              </strong>
              <span>landmark genes recognized</span>
            </div>
            <span className="status-chip status-good">Aligned</span>
          </div>
          <p className="coverage-summary-detail">
            {result.preview.n_matched} matched, {result.preview.n_missing} missing,{" "}
            {result.preview.n_extra} extra (endpoint schema{" "}
            <span className="mono">{result.schema_endpoint_id}</span>).
          </p>
          <button className="button primary" onClick={() => useUploaded(result)}>
            Use this signature
          </button>
        </div>
      )}

      <div className="advanced-row">
        <button onClick={() => setShowAdvanced((v) => !v)}>
          {showAdvanced ? "Hide advanced input" : "Advanced: paste JSON or use a demo"}
        </button>
        <span>Accepted schema: 978 landmark genes</span>
      </div>

      {showAdvanced && (
        <div className="advanced-panel">
          {demoSignatures.length > 0 && (
            <label className="advanced-field">
              <span>Bundled demo — real curated signature, illustrative only</span>
              <select value={demoId} onChange={(e) => pickDemo(e.target.value)}>
                <option value="">Select a bundled demo signature…</option>
                {demoSignatures.map((d: DemoSignature) => (
                  <option key={d.id} value={d.id}>
                    {d.label}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="advanced-field">
            <span>Signature (JSON: gene symbol → value, the 978 landmark genes)</span>
            <textarea
              value={pasteText}
              placeholder='{ "A1BG": 0.12, "A1CF": -0.4, "…": 0.0 }'
              onChange={(e) => setPasteText(e.target.value)}
            />
          </label>
          {pasteError && (
            <p role="alert" className="paste-error">
              {pasteError}
            </p>
          )}
          <button
            className="button outline"
            disabled={pasteText.trim().length === 0}
            onClick={loadPaste}
          >
            Load signature
          </button>
          <button className="button quiet" onClick={onTryMolecule}>
            Try with a known molecule
          </button>
        </div>
      )}
    </div>
  );
}

function MoleculePanel({ onUpload }: { onUpload: () => void }) {
  const [query, setQuery] = useState("");
  const [searched, setSearched] = useState(false);

  const normalized = query.trim().toLowerCase();
  const visible = normalized
    ? MOCK_MEASURED_SIGNATURES.filter(
        (s) =>
          s.molecule.toLowerCase().includes(normalized) ||
          normalized.includes(s.molecule.toLowerCase()),
      )
    : MOCK_MEASURED_SIGNATURES;

  return (
    <div className="lookup-panel">
      <div className="lookup-heading">
        <div>
          <h2>
            Find a measured signature <PlannedBadge />
          </h2>
          <p>Search by name, PubChem CID or InChIKey.</p>
        </div>
        <span>Example data</span>
      </div>

      <form
        className="search-box"
        onSubmit={(e) => {
          e.preventDefault();
          setSearched(true);
        }}
      >
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Try Estradiol or Bisphenol A"
          aria-label="Search molecule"
        />
        <button className="button primary" type="submit">
          Search
        </button>
      </form>

      {!searched ? (
        <div className="quick-examples">
          <span>Examples</span>
          {["Estradiol", "Bisphenol A", "Tamoxifen"].map((name) => (
            <button
              key={name}
              onClick={() => {
                setQuery(name);
                setSearched(true);
              }}
            >
              {name}
            </button>
          ))}
        </div>
      ) : (
        <div className="signature-results">
          <div className="result-count">
            <strong>
              {visible.length} example {visible.length === 1 ? "signature" : "signatures"}{" "}
              <MockBadge />
            </strong>
            <span>Illustrative catalogue — lookup is not connected to real data yet</span>
          </div>
          {visible.map((item) => (
            <div key={item.id} className="signature-option signature-option-static">
              <span className="signature-radio" aria-hidden />
              <span>
                <strong>{item.molecule}</strong>
                <small>{item.context}</small>
              </span>
              <span>
                <strong>{item.source}</strong>
                <small>{item.coverage}</small>
              </span>
            </div>
          ))}
          <div className="no-signature">
            <strong>Measured-signature lookup is a planned capability</strong>
            <p>
              These entries are examples only. EndoScan will not infer a result from a molecule name
              or structure alone — upload a measured transcriptomic signature to run an analysis.
            </p>
            <button className="button primary" onClick={onUpload}>
              Upload a signature instead
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
