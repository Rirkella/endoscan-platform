// Source step. Three ways to start, in priority order:
//   Upload   — REAL: upload a measured signature file (POST /signatures/parse; the server is the one
//              gene validator). Raw JSON paste is demoted to a collapsed "Advanced" disclosure.
//   Demo     — REAL: the bundled curated demo signatures. One click loads a real measured signature
//              into the same validate → run pipeline as an upload (no JSON copying required).
//   Catalogue— PREVIEW: a public-signature search is not connected to real data yet, so the search
//              is disabled and clearly marked; it points users at the real demos instead. (A molecule
//              name is never scored directly — EndoScan stays transcriptomics-first.)

import { useState } from "react";

import { EndoscanApiError, api } from "../../api/client";
import type { ParseResult, Signature } from "../../api/types";
import { demoSignatures } from "../../demo-signatures";
import { demoDisplay } from "../../demo-signatures/display";
import { MockBadge, PlannedBadge } from "../PlannedBadge";
import { ErrorNotice } from "../ErrorNotice";
import type { PreparedInput } from "../../pages/Analyze";

type Mode = "upload" | "demo" | "catalogue";

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
}: {
  onPrepared: (input: PreparedInput) => void;
  endpointCount: number;
}) {
  const [mode, setMode] = useState<Mode>("upload");

  return (
    <section className="source-layout">
      <div className="source-main">
        <div className="segmented segmented-3" role="tablist" aria-label="Input source">
          <button
            role="tab"
            aria-selected={mode === "upload"}
            className={mode === "upload" ? "selected" : ""}
            onClick={() => setMode("upload")}
          >
            Upload
          </button>
          <button
            role="tab"
            aria-selected={mode === "demo"}
            className={mode === "demo" ? "selected" : ""}
            onClick={() => setMode("demo")}
          >
            Try a demo
          </button>
          <button
            role="tab"
            aria-selected={mode === "catalogue"}
            className={mode === "catalogue" ? "selected" : ""}
            onClick={() => setMode("catalogue")}
          >
            Public catalogue
          </button>
        </div>

        {mode === "upload" && (
          <UploadPanel onPrepared={onPrepared} onTryDemo={() => setMode("demo")} />
        )}
        {mode === "demo" && <DemoPanel onPrepared={onPrepared} />}
        {mode === "catalogue" && <CataloguePanel onTryDemo={() => setMode("demo")} />}
      </div>

      <aside className="source-aside">
        <p className="aside-label">Good to know</p>
        <div className="help-card">
          <p>
            <strong>Start with a measured response.</strong> EndoScan analyzes gene-expression
            signatures, so a molecule name or structure is never scored on its own.
          </p>
          <p>
            <strong>No data of your own?</strong> Run one of the real demo signatures — it goes
            through the same checks and models as an upload.
          </p>
        </div>
      </aside>
    </section>
  );
}

function UploadPanel({
  onPrepared,
  onTryDemo,
}: {
  onPrepared: (input: PreparedInput) => void;
  onTryDemo: () => void;
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
      subtitle: "Uploaded gene-expression signature",
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
          A CSV or JSON file of landmark-gene values. We check gene coverage against the model schema
          next.
        </p>
        <span className="button primary upload-cta">{file ? file.name : "Choose data file"}</span>
        <button type="button" className="upload-demo-link" onClick={onTryDemo}>
          or try a real demo instead
        </button>
      </label>

      {parsing && <p className="upload-status">Checking the file…</p>}
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
            <span className="status-chip status-good">Ready</span>
          </div>
          <p className="coverage-summary-detail">
            {result.preview.n_matched} matched, {result.preview.n_missing} missing,{" "}
            {result.preview.n_extra} extra (schema <span className="mono">{result.schema_endpoint_id}</span>
            ).
          </p>
          <button className="button primary" onClick={() => useUploaded(result)}>
            Use this signature
          </button>
        </div>
      )}

      <div className="advanced-row">
        <button onClick={() => setShowAdvanced((v) => !v)}>
          {showAdvanced ? "Hide advanced input" : "Advanced: paste JSON"}
        </button>
        <span>978 landmark genes</span>
      </div>

      {showAdvanced && (
        <div className="advanced-panel">
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
        </div>
      )}
    </div>
  );
}

function DemoPanel({ onPrepared }: { onPrepared: (input: PreparedInput) => void }) {
  if (demoSignatures.length === 0) {
    return (
      <div className="demo-panel">
        <div className="no-signature">
          <strong>No demo signatures are bundled in this build</strong>
          <p>Upload a measured signature file, or paste one as JSON under the Upload tab.</p>
        </div>
      </div>
    );
  }

  function runDemo(id: string) {
    const demo = demoSignatures.find((d) => d.id === id);
    if (!demo) return;
    const d = demoDisplay(demo);
    onPrepared({
      title: d.name,
      subtitle: `Real demo signature · ${d.source}`,
      kind: "demo",
      signature: demo.signature,
      parse: null,
    });
  }

  return (
    <div className="demo-panel">
      <div className="demo-panel-head">
        <div>
          <h2>Try a demo analysis</h2>
          <p>
            These are real, curated measured signatures. Pick one and run it through the full
            analysis — no data of your own required.
          </p>
        </div>
        <span className="status-chip status-good">Real curated data</span>
      </div>
      <div className="demo-grid">
        {demoSignatures.map((demo) => {
          const d = demoDisplay(demo);
          return (
            <article className="demo-card" key={demo.id}>
              <h3>{d.name}</h3>
              <p className="demo-card-note">{d.demonstrates}</p>
              <dl className="demo-card-meta">
                <div>
                  <dt>Context</dt>
                  <dd>{d.context}</dd>
                </div>
                <div>
                  <dt>Source</dt>
                  <dd>{d.source}</dd>
                </div>
                <div>
                  <dt>Identifier</dt>
                  <dd className="mono">{d.identifier}</dd>
                </div>
              </dl>
              <button className="button primary full-button" onClick={() => runDemo(demo.id)}>
                Use this demo
              </button>
            </article>
          );
        })}
      </div>
      <p className="demo-panel-foot">
        Real measured signatures (LINCS Level 5). They are illustrative examples, not a claim about
        any specific product or exposure.
      </p>
    </div>
  );
}

function CataloguePanel({ onTryDemo }: { onTryDemo: () => void }) {
  return (
    <div className="lookup-panel">
      <div className="lookup-heading">
        <div>
          <h2>
            Public signature catalogue <PlannedBadge label="Preview" />
          </h2>
          <p>Search public measured signatures by molecule, PubChem CID or InChIKey.</p>
        </div>
        <span>Preview</span>
      </div>

      <form className="search-box" onSubmit={(e) => e.preventDefault()}>
        <input
          placeholder="Search is not connected yet"
          aria-label="Search molecule"
          disabled
        />
        <button className="button primary" type="submit" disabled>
          Search
        </button>
      </form>

      <div className="no-signature">
        <strong>Molecule search is not available yet <MockBadge label="Preview" /></strong>
        <p>
          A searchable catalogue that maps a molecule to its measured public signatures is planned,
          but the underlying signature store and search API are not part of this build. Nothing here
          returns real results, so the search is disabled rather than showing placeholder data.
        </p>
        <p>
          To run a real analysis today, use one of the bundled demo signatures, or upload your own
          measured signature.
        </p>
        <button className="button primary" onClick={onTryDemo}>
          Try a real demo instead
        </button>
      </div>
    </div>
  );
}
