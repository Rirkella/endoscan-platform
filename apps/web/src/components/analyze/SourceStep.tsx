// Source step. Three ways to start, in priority order:
//   Upload   — REAL: upload a measured signature file (POST /signatures/parse; the server is the one
//              gene validator). Raw JSON paste is demoted to a collapsed "Advanced" disclosure.
//   Demo     — REAL: the bundled curated demo signatures. One click loads a real measured signature
//              into the same validate → run pipeline as an upload (no JSON copying required).
//   Catalogue— REAL: search verified identities with committed public measured signatures, select
//              one, and pass it through the common validator. A molecule name is never scored
//              directly — EndoScan stays transcriptomics-first.

import { type FormEvent, useState } from "react";

import { api } from "../../api/client";
import type { CatalogueCompound, ParseResult } from "../../api/types";
import { demoSignatures } from "../../demo-signatures";
import { demoDisplay } from "../../demo-signatures/display";
import { exampleSignatureFiles, type ExampleSignatureFile } from "../../example-files";
import { ErrorNotice } from "../ErrorNotice";
import type { PreparedInput } from "../../pages/Analyze";

type Mode = "upload" | "demo" | "catalogue";

type InputFormat = "json" | "csv" | "tsv";

function formatOf(name: string): InputFormat | null {
  const n = name.toLowerCase();
  if (n.endsWith(".json")) return "json";
  if (n.endsWith(".csv")) return "csv";
  if (n.endsWith(".tsv")) return "tsv";
  return null;
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
        {mode === "catalogue" && <CataloguePanel onPrepared={onPrepared} />}
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
  const [format, setFormat] = useState<InputFormat | null>(null);
  const [parsing, setParsing] = useState(false);
  const [result, setResult] = useState<ParseResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [allowExtra, setAllowExtra] = useState(false);
  const [exampleLoading, setExampleLoading] = useState<string | null>(null);

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);

  async function doParse(
    f: File,
    fmt: InputFormat,
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
      setError(new Error("Please upload a .json, .csv or .tsv file."));
      return;
    }
    void doParse(f, fmt);
  }

  async function loadExample(example: ExampleSignatureFile) {
    setExampleLoading(example.id);
    setError(null);
    setResult(null);
    setAllowExtra(false);
    try {
      const response = await fetch(example.url, { headers: { accept: "text/plain" } });
      if (!response.ok) throw new Error("The example file could not be loaded.");
      const blob = await response.blob();
      const exampleFile = new File([blob], example.filename, {
        type: example.format === "csv" ? "text/csv" : "text/tab-separated-values",
      });
      setFile(exampleFile);
      setFormat(example.format);
      await doParse(exampleFile, example.format);
    } catch (e) {
      setError(e);
    } finally {
      setExampleLoading(null);
    }
  }

  function useUploaded(r: ParseResult) {
    if (!r.signature) return;
    onPrepared({
      title: file?.name ?? "Uploaded signature",
      subtitle: "Uploaded gene-expression signature",
      kind: "file",
      signature: r.signature,
      parse: r,
      allowExtra,
    });
  }

  async function loadPaste() {
    try {
      const parsed = await api.parseSignature({ content: pasteText, format: "json" });
      if (!parsed.signature) throw new Error("Select a sample before continuing.");
      setPasteError(null);
      onPrepared({
        title: "Pasted signature",
        subtitle: "Signature entered as JSON",
        kind: "paste",
        signature: parsed.signature,
        parse: parsed,
        allowExtra: false,
      });
    } catch (e) {
      setPasteError(e instanceof Error ? e.message : "Invalid JSON.");
    }
  }

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
          A CSV, TSV or JSON file of gene values. We check it against every endpoint schema
          next.
        </p>
        <span className="button primary upload-cta">{file ? file.name : "Choose data file"}</span>
      </label>
      <button type="button" className="upload-demo-link" onClick={onTryDemo}>
        or try a real demo instead
      </button>

      <section className="example-files" aria-labelledby="example-files-title">
        <div>
          <h3 id="example-files-title">Use a real example file</h3>
          <p>These measured examples are sent through the same multipart upload path as your file.</p>
        </div>
        {exampleSignatureFiles.map((example) => (
          <article key={example.id} className="example-file-card">
            <div>
              <strong>{example.name}</strong>
              <small>978 measured landmark-gene values</small>
            </div>
            <button
              type="button"
              className="button outline"
              disabled={exampleLoading != null}
              onClick={() => void loadExample(example)}
            >
              {exampleLoading === example.id ? "Loading…" : "Use example file"}
            </button>
            <a href={example.url} download={example.filename}>Download</a>
            <details>
              <summary>Provenance</summary>
              <p>{example.provenance}</p>
            </details>
          </article>
        ))}
      </section>

      {parsing && <p className="upload-status">Checking the file…</p>}
      {error != null && <ErrorNotice error={error} />}

      {result?.compatibility.some((item) => item.n_extra > 0) && file && format && (
        <label className="extras-toggle">
          <input
            type="checkbox"
            checked={allowExtra}
            onChange={(event) => {
              const checked = event.target.checked;
              setAllowExtra(checked);
              void doParse(file, format, { allow_extra: checked });
            }}
          />
          Ignore extra genes independently for each endpoint schema
        </label>
      )}

      {result?.preview.needs_sample && file && format && (
        <div className="sample-picker">
          <p>{result.preview.samples?.length} samples found — pick one:</p>
          <div className="sample-buttons">
            {result.preview.samples?.map((s) => (
              <button
                key={s}
                onClick={() => void doParse(file, format, { sample: s, allow_extra: allowExtra })}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {result?.signature && (
        <div className="coverage-summary">
          <div className="coverage-summary-top">
            <div>
              <strong className="tabular">{result.compatible_endpoint_ids.length}</strong>
              <span>compatible endpoint model{result.compatible_endpoint_ids.length === 1 ? "" : "s"}</span>
            </div>
            <span className={`status-chip ${result.compatible_endpoint_ids.length ? "status-good" : "status-note"}`}>
              {result.compatible_endpoint_ids.length ? "Ready" : "No compatible endpoints"}
            </span>
          </div>
          <p className="coverage-summary-detail">
            {result.preview.n_detected} genes parsed. Each registered endpoint was checked against
            its own feature schema.
          </p>
          <button className="button primary" disabled={!result.compatible_endpoint_ids.length} onClick={() => useUploaded(result)}>
            Use this signature
          </button>
        </div>
      )}

      <div className="advanced-row">
        <button onClick={() => setShowAdvanced((v) => !v)}>
          {showAdvanced ? "Hide advanced input" : "Advanced: paste JSON"}
        </button>
        <span>Endpoint-specific feature schemas</span>
      </div>

      {showAdvanced && (
        <div className="advanced-panel">
          <label className="advanced-field">
            <span>Signature (JSON: gene symbol → finite numeric value)</span>
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
            onClick={() => void loadPaste()}
          >
            Load signature
          </button>
        </div>
      )}
    </div>
  );
}

function DemoPanel({ onPrepared }: { onPrepared: (input: PreparedInput) => void }) {
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
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

  async function runDemo(id: string) {
    const demo = demoSignatures.find((d) => d.id === id);
    if (!demo) return;
    const d = demoDisplay(demo);
    setLoading(id);
    setError(null);
    try {
      const parsed = await api.parseSignature({
        content: JSON.stringify(demo.signature),
        format: "json",
      });
      if (!parsed.signature) throw new Error("The demo signature could not be prepared.");
      onPrepared({
        title: d.name,
        subtitle: `Real demo signature · ${d.source}`,
        kind: "demo",
        signature: parsed.signature,
        parse: parsed,
        allowExtra: false,
      });
    } catch (e) {
      setError(e);
    } finally {
      setLoading(null);
    }
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
              <button className="button primary full-button" disabled={loading != null} onClick={() => void runDemo(demo.id)}>
                {loading === demo.id ? "Checking…" : "Use this demo"}
              </button>
            </article>
          );
        })}
      </div>
      {error != null && <ErrorNotice error={error} />}
      <p className="demo-panel-foot">
        Real measured signatures (LINCS Level 5). They are illustrative examples, not a claim about
        any specific product or exposure.
      </p>
    </div>
  );
}

function CataloguePanel({ onPrepared }: { onPrepared: (input: PreparedInput) => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<CatalogueCompound[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function search(event?: FormEvent) {
    event?.preventDefault();
    if (query.trim().length < 2) return;
    setSearching(true);
    setError(null);
    setResults(null);
    setSelected(null);
    try {
      const response = await api.searchCatalogue(query.trim());
      setResults(response.results);
      if (response.results.length === 1 && response.results[0].signatures.length === 1) {
        setSelected(response.results[0].signatures[0].signature_id);
      }
    } catch (e) {
      setError(e);
    } finally {
      setSearching(false);
    }
  }

  async function useSignature() {
    if (!selected) return;
    setPreparing(true);
    setError(null);
    try {
      const detail = await api.getCatalogueSignature(selected);
      const parsed = await api.parseSignature({
        content: JSON.stringify(detail.signature),
        format: "json",
      });
      if (!parsed.signature) throw new Error("The measured signature could not be prepared.");
      onPrepared({
        title: detail.compound_name,
        subtitle: `${detail.dataset} ${detail.processing_level} · ${detail.cell_lines.join(" + ")}`,
        kind: "catalogue",
        signature: parsed.signature,
        parse: parsed,
        allowExtra: false,
      });
    } catch (e) {
      setError(e);
    } finally {
      setPreparing(false);
    }
  }

  return (
    <div className="lookup-panel">
      <div className="lookup-heading">
        <div>
          <h2>Public measured-signature catalogue</h2>
          <p>Search public measured signatures by molecule, PubChem CID or InChIKey.</p>
        </div>
        <span>Verified identities</span>
      </div>

      <form className="search-box" onSubmit={(event) => void search(event)}>
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="e.g. caffeic acid, 689043 or InChIKey"
          aria-label="Search molecule"
        />
        <button
          className="button primary"
          type="submit"
          disabled={searching || query.trim().length < 2}
        >
          {searching ? "Searching…" : "Search"}
        </button>
      </form>
      <div className="quick-examples" aria-label="Example searches">
        <span>Try:</span>
        {["Caffeic Acid", "Closantel", "2335"].map((example) => (
          <button key={example} type="button" onClick={() => setQuery(example)}>
            {example}
          </button>
        ))}
      </div>

      {error != null && <ErrorNotice error={error} />}
      {results != null && (
        <section className="signature-results" aria-live="polite">
          <div className="result-count">
            <strong>{results.length} verified compound{results.length === 1 ? "" : "s"}</strong>
            <span>Only records with measured signatures are shown</span>
          </div>
          {results.length === 0 ? (
            <div className="no-signature">
              <strong>No measured public signature found</strong>
              <p>
                Try a different name, PubChem CID or full InChIKey. No inferred molecule-only
                result is substituted.
              </p>
            </div>
          ) : (
            <div className="catalogue-results">
              {results.flatMap((compound) =>
                compound.signatures.map((signature) => (
                  <label
                    className={`catalogue-result ${selected === signature.signature_id ? "catalogue-result-selected" : ""}`}
                    key={signature.signature_id}
                  >
                    <input
                      type="radio"
                      name="catalogue-signature"
                      checked={selected === signature.signature_id}
                      onChange={() => setSelected(signature.signature_id)}
                    />
                    <span className="signature-radio" aria-hidden="true" />
                    <span className="catalogue-result-main">
                      <strong>{compound.preferred_name}</strong>
                      <small>
                        PubChem CID {compound.pubchem_cid} · {compound.compound_id}
                      </small>
                      <span className="mono catalogue-smiles">{compound.isomeric_smiles}</span>
                    </span>
                    <span className="catalogue-result-meta">
                      <strong>{signature.dataset} · {signature.accession}</strong>
                      <small>{signature.processing_level} · {signature.n_genes} genes</small>
                      <small>{signature.cell_lines.join(" + ")} · dose/time not available</small>
                      <small>{signature.aggregation}</small>
                    </span>
                  </label>
                )),
              )}
              <button
                type="button"
                className="button primary use-signature"
                disabled={!selected || preparing}
                onClick={() => void useSignature()}
              >
                {preparing ? "Checking measured signature…" : "Use selected measured signature"}
              </button>
            </div>
          )}
        </section>
      )}

      <p className="catalogue-honesty">
        Identity selects a linked measured transcriptomic record; EndoScan analyzes that signature,
        never the name or structure alone. Missing dose and time metadata remain explicitly
        unavailable.
      </p>
    </div>
  );
}
