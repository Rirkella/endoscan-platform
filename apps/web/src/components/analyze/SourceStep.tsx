// Source step. Upload and the verified public catalogue are the only entry points.
// Every selected signature is serialized as a File and sent through multipart /signatures/parse.

import { type FormEvent, useState } from "react";

import { api } from "../../api/client";
import type { CatalogueCompound, InputValueType, ParseResult } from "../../api/types";
import { exampleSignatureFiles, type ExampleSignatureFile } from "../../example-files";
import { ErrorNotice } from "../ErrorNotice";
import type { PreparedInput } from "../../pages/Analyze";

type Mode = "upload" | "catalogue";

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
        <div className="segmented" role="tablist" aria-label="Input source">
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
            aria-selected={mode === "catalogue"}
            className={mode === "catalogue" ? "selected" : ""}
            onClick={() => setMode("catalogue")}
          >
            Public catalogue
          </button>
        </div>

        {mode === "upload" && <UploadPanel onPrepared={onPrepared} />}
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
            <strong>No data of your own?</strong> Use one of the named real example files — it goes
            through the same checks and models as an upload.
          </p>
        </div>
      </aside>
    </section>
  );
}

function UploadPanel({ onPrepared }: { onPrepared: (input: PreparedInput) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [format, setFormat] = useState<InputFormat | null>(null);
  const [parsing, setParsing] = useState(false);
  const [result, setResult] = useState<ParseResult | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [allowExtra, setAllowExtra] = useState(false);
  const [valueType, setValueType] = useState<InputValueType>("raw_expression");
  const [exampleLoading, setExampleLoading] = useState<string | null>(null);
  const [selectedExample, setSelectedExample] = useState<ExampleSignatureFile | null>(null);
  const [referenceDescription, setReferenceDescription] = useState("");
  const [conditionDescription, setConditionDescription] = useState("");

  const [showAdvanced, setShowAdvanced] = useState(false);
  const [pasteText, setPasteText] = useState("");
  const [pasteError, setPasteError] = useState<string | null>(null);

  async function doParse(
    f: File,
    fmt: InputFormat,
    opts: {
      allow_extra?: boolean;
      sample?: string;
      input_value_type?: InputValueType;
    } = {},
  ) {
    setParsing(true);
    setError(null);
    setResult(null);
    try {
      const r = await api.parseSignature({
        file: f,
        format: fmt,
        input_value_type: opts.input_value_type ?? valueType,
        ...opts,
      });
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
    setSelectedExample(null);
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
      setSelectedExample(example);
      setFormat(example.format);
      setValueType("differential_zscore");
      await doParse(exampleFile, example.format, { input_value_type: "differential_zscore" });
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
      inputValueType: r.input_value_type,
      profileType: selectedExample ? "Cross-cell aggregated measured example" : "User-supplied signature",
      valueTypeLabel: selectedExample ? "LINCS Level 5 differential z-score" : undefined,
      referenceComparison: selectedExample
        ? "Calculated during LINCS processing using the corresponding experimental controls. EndoScan receives the resulting differential signature and does not choose the control."
        : referenceDescription.trim() || undefined,
      experimentalContext: selectedExample
        ? `${selectedExample.dataset} ${selectedExample.processing_level} · ${selectedExample.cell_lines.join(" + ")}`
        : conditionDescription.trim() || undefined,
      cellModels: selectedExample
        ? ["MCF7 — human breast cancer cell line", "A549 — human lung adenocarcinoma cell line"]
        : undefined,
      aggregationWarning: selectedExample
        ? "This reference profile combines responses from multiple cell models. Opposing cell-specific changes may be attenuated in the aggregate."
        : undefined,
    });
  }

  async function loadPaste() {
    try {
      const parsed = await api.parseSignature({
        content: pasteText,
        format: "json",
        input_value_type: valueType,
      });
      if (!parsed.signature) throw new Error("Select a sample before continuing.");
      setPasteError(null);
      onPrepared({
        title: "Pasted signature",
        subtitle: "Signature entered as JSON",
        kind: "paste",
        signature: parsed.signature,
        parse: parsed,
        allowExtra: false,
        inputValueType: parsed.input_value_type,
      });
    } catch (e) {
      setPasteError(e instanceof Error ? e.message : "Invalid JSON.");
    }
  }

  return (
    <div className="upload-panel-wrap">
      <label className="signature-value-type">
        <span>What do the values represent?</span>
        <select
          value={valueType}
          onChange={(event) => {
            const next = event.target.value as InputValueType;
            setValueType(next);
            if (file && format) void doParse(file, format, { input_value_type: next });
          }}
        >
          <option value="differential_zscore">Differential z-score</option>
          <option value="log2_fold_change">Log2 fold change</option>
          <option value="ranked_statistic">Signed ranked statistic</option>
          <option value="raw_expression">Raw expression (no matched reference)</option>
        </select>
        <small>This determines whether increased and decreased pathway response can be interpreted.</small>
      </label>
      <div className="upload-context-fields">
        <label>
          <span>Reference/control description</span>
          <input
            value={referenceDescription}
            onChange={(event) => setReferenceDescription(event.target.value)}
            placeholder="How were differential values calculated?"
          />
          <small>Required for user-supplied differential values. EndoScan does not select a control.</small>
        </label>
        <label>
          <span>Sample or condition</span>
          <input
            value={conditionDescription}
            onChange={(event) => setConditionDescription(event.target.value)}
            placeholder="Cell model, dose, exposure time, perturbation (when known)"
          />
        </label>
      </div>
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

      <section className="example-files" aria-labelledby="example-files-title">
        <div>
          <h3 id="example-files-title">Use a real example file</h3>
          <p>These measured examples are sent through the same multipart upload path as your file.</p>
        </div>
        {exampleSignatureFiles.map((example) => (
          <article key={example.id} className="example-file-card">
            <div>
              <strong>{example.name}</strong>
              <small>PubChem CID {example.pubchem_cid} · {example.format.toUpperCase()}</small>
              <small>{example.dataset} {example.processing_level} · {example.cell_lines.join(" + ")}</small>
              <small>{example.dose ?? "Dose unavailable"} · {example.timepoint ?? "Time unavailable"}</small>
              <span className="example-result-badge">{example.expected_result}</span>
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
              <p>Measured {example.processing_level} signature from {example.dataset} ({example.accession}).</p>
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
          <button
            className="button primary"
            disabled={
              !result.compatible_endpoint_ids.length ||
              (!selectedExample && valueType !== "raw_expression" && !referenceDescription.trim())
            }
            onClick={() => useUploaded(result)}
          >
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
        setSelected(`catalogue:${response.results[0].signatures[0].signature_id}`);
      } else if (response.results.length === 1 && response.results[0].reference_contexts.length) {
        setSelected(`reference:${response.results[0].reference_contexts[0]}:${response.results[0].compound_id}`);
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
      if (selected.startsWith("reference:")) {
        const [, context, compoundId] = selected.split(":", 3);
        const detail = await api.getReferenceSignature(context, compoundId);
        const referenceFile = new File([JSON.stringify(detail.signature)], `${compoundId}.json`, {
          type: "application/json",
        });
        const parsed = await api.parseSignature({
          file: referenceFile,
          format: "json",
          input_value_type: detail.value_type,
        });
        if (!parsed.signature) throw new Error("The aggregated reference profile could not be prepared.");
        onPrepared({
          title: detail.preferred_name ?? detail.compound_id,
          subtitle: `${detail.profile_type} · ${detail.context} reference set`,
          kind: "reference",
          signature: parsed.signature,
          parse: parsed,
          allowExtra: false,
          inputValueType: parsed.input_value_type,
          profileType: detail.profile_type,
          valueTypeLabel: detail.value_type_label,
          referenceComparison: detail.reference_comparison,
          aggregationWarning: detail.aggregate_pathway_warning,
          cellModels: detail.cell_models,
          experimentalContext: detail.aggregation_description,
        });
        return;
      }
      const signatureId = selected.replace(/^catalogue:/, "");
      const detail = await api.getCatalogueSignature(signatureId);
      const catalogueFile = new File(
        [JSON.stringify(detail.signature)],
        `${detail.signature_id}.json`,
        { type: "application/json" },
      );
      const parsed = await api.parseSignature({
        file: catalogueFile,
        format: "json",
        input_value_type: "differential_zscore",
      });
      if (!parsed.signature) throw new Error("The measured signature could not be prepared.");
      onPrepared({
        title: detail.compound_name,
        subtitle: `${detail.dataset} ${detail.processing_level} · ${detail.cell_lines.join(" + ")}`,
        kind: "catalogue",
        signature: parsed.signature,
        parse: parsed,
        allowExtra: false,
        inputValueType: parsed.input_value_type,
        valueTypeLabel: "LINCS Level 5 differential z-score",
        referenceComparison: "Calculated during LINCS processing using the corresponding experimental controls. EndoScan receives the resulting differential signature and does not choose the control.",
        profileType: "Cross-cell aggregated measured signature",
        aggregationWarning: "This reference profile combines responses from multiple cell models. Opposing cell-specific changes may be attenuated in the aggregate.",
        cellModels: ["MCF7 — human breast cancer cell line", "A549 — human lung adenocarcinoma cell line"],
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
          <h2>Public compound and reference catalogue</h2>
          <p>Search known identities and compatible reference profiles by name, synonym, PubChem CID, InChIKey, SMILES or source ID.</p>
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
        {["Caffeic Acid", "Bisphenol A", "Closantel", "2335"].map((example) => (
          <button key={example} type="button" onClick={() => setQuery(example)}>
            {example}
          </button>
        ))}
      </div>

      {error != null && <ErrorNotice error={error} />}
      {results != null && (
        <section className="signature-results" aria-live="polite">
          <div className="result-count">
            <strong>{results.length} canonical compound result{results.length === 1 ? "" : "s"}</strong>
            <span>Reference profiles, source identities and measured signatures share one index</span>
          </div>
          {results.length === 0 ? (
            <div className="no-signature">
              <strong>No known compound identity or compatible reference record matched</strong>
              <p>
                Try a different name, synonym, PubChem CID, full InChIKey, SMILES or source ID.
                No inferred molecule-only result is substituted.
              </p>
            </div>
          ) : (
            <div className="catalogue-results">
              {results.map((compound) => {
                const signature = compound.signatures[0];
                const choice = signature
                  ? `catalogue:${signature.signature_id}`
                  : compound.reference_contexts.length
                    ? `reference:${compound.reference_contexts[0]}:${compound.compound_id}`
                    : null;
                return (
                  <label
                    className={`catalogue-result ${choice && selected === choice ? "catalogue-result-selected" : ""} ${choice ? "" : "catalogue-result-unavailable"}`}
                    key={compound.compound_id}
                  >
                    {choice ? <input type="radio" name="catalogue-signature" checked={selected === choice} onChange={() => setSelected(choice)} /> : <span />}
                    {choice && <span className="signature-radio" aria-hidden="true" />}
                    <span className="catalogue-result-main">
                      <strong>{compound.preferred_name}</strong>
                      <small>{compound.pubchem_cid ? `PubChem CID ${compound.pubchem_cid} · ` : ""}{compound.compound_id}</small>
                      {compound.isomeric_smiles && <span className="mono catalogue-smiles">{compound.isomeric_smiles}</span>}
                    </span>
                    <span className="catalogue-result-meta">
                      <strong>{compound.availability_status}</strong>
                      {signature && <small>{signature.dataset} · {signature.processing_level} · {signature.n_genes} genes</small>}
                      {!signature && compound.reference_contexts.length > 0 && <small>Available in {compound.reference_contexts.join(" and ")} reference map</small>}
                      {compound.availability_reason && <small>{compound.availability_reason}</small>}
                    </span>
                  </label>
                );
              })}
              <button
                type="button"
                className="button primary use-signature"
                disabled={!selected || preparing}
                onClick={() => void useSignature()}
              >
                {preparing ? "Checking measured signature…" : selected?.startsWith("reference:") ? "Use selected aggregated reference profile" : "Use selected measured signature"}
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
