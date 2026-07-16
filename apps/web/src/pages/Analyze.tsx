// Analyze — the primary flow, ported from prototype-v2's stepped workspace (source → validate →
// running → result) and wired to the REAL API underneath:
//   source     : upload a signature/example file or select a verified measured public signature.
//                Every path uses multipart POST /signatures/parse; identity alone is never scored.
//   validate   : real gene coverage (from the parse preview) + real endpoint compatibility (/endpoints).
//   running    : shown while POST /analyze is in flight.
//   result     : overview (real scores/calls/thresholds/status), evidence (real /explain + pathways +
//                limitations), reference (real /explore/locate), report (PLANNED preview of exports).
// The endpoint set is always whatever /endpoints returns — never hardcoded ER/AR. Wording stays
// honest: endpoint signal score / model call (Active/Inactive), below-threshold is neutral, and the
// report/known-molecule areas are explicitly badged as not-yet-available.

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import {
  predictionScore,
  type EndpointCompatibility,
  type EndpointSummary,
  type InputValueType,
  type ParseResult,
  type Signature,
} from "../api/types";
import { PlannedBadge } from "../components/PlannedBadge";
import { ErrorNotice } from "../components/ErrorNotice";
import { EvidencePanel } from "../components/analyze/EvidencePanel";
import { BiologicalResponsePanel } from "../components/analyze/BiologicalResponsePanel";
import { ReferencePanel } from "../components/analyze/ReferencePanel";
import { SourceStep } from "../components/analyze/SourceStep";
import { ValidationStep } from "../components/analyze/ValidationStep";
import { type EndpointSignal, useAnalyze } from "../hooks/useAnalyze";
import { useAsync } from "../hooks/useAsync";

export type AnalyzeStep = "source" | "validate" | "running" | "result";
export type ResultTab = "overview" | "biological" | "endpoint" | "similar" | "report";

// What the user prepared, for display in validate/result headers. `signature` is the REAL payload.
export interface PreparedInput {
  title: string;
  subtitle: string;
  kind: "file" | "paste" | "catalogue" | "reference";
  signature: Signature;
  parse: ParseResult;
  allowExtra: boolean;
  inputValueType: InputValueType;
  profileType?: string;
  valueTypeLabel?: string;
  referenceComparison?: string;
  aggregationWarning?: string;
  cellModels?: string[];
  experimentalContext?: string;
}

export function Analyze() {
  const [searchParams] = useSearchParams();
  const catalogueSignatureId = searchParams.get("catalogue_signature");
  const referenceContext = searchParams.get("reference_context");
  const referenceCompound = searchParams.get("reference_compound");
  const catalogueHandled = useRef(false);
  const referenceHandled = useRef(false);
  const endpoints = useAsync(() => api.listEndpoints(), []);
  const endpointList = endpoints.data ?? [];
  const analyze = useAnalyze(endpointList);

  const [step, setStep] = useState<AnalyzeStep>("source");
  const [prepared, setPrepared] = useState<PreparedInput | null>(null);
  const [resultTab, setResultTab] = useState<ResultTab>("overview");
  const [runError, setRunError] = useState<unknown>(null);
  const [catalogueLoading, setCatalogueLoading] = useState(false);
  const [catalogueError, setCatalogueError] = useState<unknown>(null);

  useEffect(() => {
    if (!catalogueSignatureId || catalogueHandled.current) return;
    catalogueHandled.current = true;
    setCatalogueLoading(true);
    setCatalogueError(null);
    void api
      .getCatalogueSignature(catalogueSignatureId)
      .then(async (detail) => {
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
        if (!parsed.signature) throw new Error("The selected measured signature could not be prepared.");
        onPrepared({
          title: detail.compound_name,
          subtitle: `${detail.dataset} ${detail.processing_level} · ${detail.cell_lines.join(" + ")}`,
          kind: "catalogue",
          signature: parsed.signature,
          parse: parsed,
          allowExtra: false,
          inputValueType: parsed.input_value_type,
        });
      })
      .catch(setCatalogueError)
      .finally(() => setCatalogueLoading(false));
  }, [catalogueSignatureId]);

  useEffect(() => {
    if (!referenceContext || !referenceCompound || referenceHandled.current) return;
    referenceHandled.current = true;
    setCatalogueLoading(true);
    setCatalogueError(null);
    void api
      .getReferenceSignature(referenceContext, referenceCompound)
      .then(async (detail) => {
        const referenceFile = new File(
          [JSON.stringify(detail.signature)],
          `${detail.context}-${detail.compound_id}-aggregated-reference.json`,
          { type: "application/json" },
        );
        const parsed = await api.parseSignature({
          file: referenceFile,
          format: "json",
          input_value_type: detail.value_type,
        });
        if (!parsed.signature) throw new Error("The reference support vector could not be prepared.");
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
      })
      .catch(setCatalogueError)
      .finally(() => setCatalogueLoading(false));
  }, [referenceContext, referenceCompound]);

  function onPrepared(input: PreparedInput) {
    setPrepared(input);
    setStep("validate");
  }

  async function onRun() {
    if (!prepared) return;
    setStep("running");
    setRunError(null);
    try {
      await analyze.run(
        prepared.signature,
        prepared.parse.compatible_endpoint_ids,
        prepared.allowExtra,
        prepared.inputValueType,
      );
    } catch (e) {
      setRunError(e);
    } finally {
      setResultTab("overview");
      setStep("result");
    }
  }

  function reset() {
    setStep("source");
    setPrepared(null);
    setResultTab("overview");
    setRunError(null);
  }

  return (
    <div className="analyze-page">
      {step !== "result" && (
        <>
          <div className="page-header">
            <div>
              <p className="eyebrow">New screening</p>
              <h1>Analyze a gene-expression signature</h1>
              <p className="page-copy">
                Start with a measured biological response — upload your own or use a named real example file. Every
                input is checked before the models run. Results are experimental endpoint signals,
                not clinical, regulatory or safety conclusions.
              </p>
            </div>
          </div>
          <WorkflowSteps current={step} />
        </>
      )}

      {step === "source" && (
        <>
          {catalogueLoading && <p className="check-plain">Loading the selected measured signature…</p>}
          {catalogueError != null && <ErrorNotice error={catalogueError} />}
          {!catalogueLoading && (
            <SourceStep onPrepared={onPrepared} endpointCount={endpointList.length} />
          )}
        </>
      )}

      {step === "validate" && prepared && (
        <ValidationStep
          input={prepared}
          onBack={() => setStep("source")}
          onRun={onRun}
        />
      )}

      {step === "running" && prepared && <RunningStep title={prepared.title} />}

      {step === "result" && prepared && (
        <ResultWorkspace
          input={prepared}
          endpoints={endpointList}
          signals={analyze.signals}
          allFailed={analyze.summary?.status === "all_failed"}
          runError={runError}
          tab={resultTab}
          setTab={setResultTab}
          onNew={reset}
        />
      )}
    </div>
  );
}

function WorkflowSteps({ current }: { current: AnalyzeStep }) {
  const order: AnalyzeStep[] = ["source", "validate", "running", "result"];
  const currentIndex = order.indexOf(current);
  const labels = ["Add input", "Check compatibility", "Run models", "Review results"];
  return (
    <ol className="workflow-steps" aria-label="Analysis progress">
      {labels.map((label, index) => (
        <li key={label} className={index <= currentIndex ? "step-active" : ""}>
          <span>{index < currentIndex ? "OK" : index + 1}</span>
          <strong>{label}</strong>
        </li>
      ))}
    </ol>
  );
}

function RunningStep({ title }: { title: string }) {
  return (
    <section className="running-panel">
      <div className="running-indicator">
        <span />
        <span />
        <span />
      </div>
      <p className="eyebrow">Analysis in progress</p>
      <h2>Analyzing {title}</h2>
      <p>Running the available endpoint models on your signature and preparing the evidence.</p>
    </section>
  );
}

function ResultWorkspace({
  input,
  endpoints,
  signals,
  allFailed,
  runError,
  tab,
  setTab,
  onNew,
}: {
  input: PreparedInput;
  endpoints: EndpointSummary[];
  signals: EndpointSignal[];
  allFailed: boolean;
  runError: unknown;
  tab: ResultTab;
  setTab: (t: ResultTab) => void;
  onNew: () => void;
}) {
  const scored = signals.filter((s) => s.result != null);
  const nAbove = scored.filter((s) => s.result!.call).length;

  return (
    <div className="result-workspace">
      <div className="result-header">
        <div>
          <button className="back-link" onClick={onNew}>
            Back to new analysis
          </button>
          <p className="eyebrow">Transcriptomic analysis result</p>
          <h1>{input.title}</h1>
          <p>{input.subtitle}</p>
        </div>
        <div className="result-actions">
          <button className="button primary" onClick={() => setTab("report")}>
            Preview report
          </button>
        </div>
      </div>

      <nav className="result-tabs" aria-label="Result sections" role="tablist">
        {([
          ["overview", "Overview"],
          ["biological", "Biological response"],
          ["endpoint", "Endpoint evidence"],
          ["similar", "Similar signatures"],
          ["report", "Report"],
        ] as [ResultTab, string][]).map(([item, label]) => (
          <button
            key={item}
            role="tab"
            aria-selected={tab === item}
            className={tab === item ? "result-tab-active" : ""}
            onClick={() => setTab(item)}
          >
            {label}
          </button>
        ))}
      </nav>

      {tab === "overview" && (
        <OverviewTab
          input={input}
          signals={signals}
          nAbove={nAbove}
          nScored={scored.length}
          runError={runError}
          allFailed={allFailed}
          onEvidence={() => setTab("endpoint")}
          onReference={() => setTab("similar")}
        />
      )}
      {tab === "biological" && (
        <BiologicalResponsePanel
          signature={input.signature}
          inputValueType={input.inputValueType}
          aggregationWarning={input.aggregationWarning}
        />
      )}
      {tab === "endpoint" && (
        <EvidencePanel
          signals={signals}
          signature={input.signature}
          compound={input.kind === "catalogue" || input.kind === "reference" ? input.title : undefined}
        />
      )}
      {tab === "similar" && <ReferencePanel endpoints={endpoints} signature={input.signature} />}
      {tab === "report" && <ReportTab input={input} signals={signals} nAbove={nAbove} />}
    </div>
  );
}

function OverviewTab({
  input,
  signals,
  nAbove,
  nScored,
  runError,
  allFailed,
  onEvidence,
  onReference,
}: {
  input: PreparedInput;
  signals: EndpointSignal[];
  nAbove: number;
  nScored: number;
  runError: unknown;
  allFailed: boolean;
  onEvidence: () => void;
  onReference: () => void;
}) {
  return (
    <div className="overview-layout">
      <div className="overview-main">
        <section className="executive-summary">
          <div>
            <p className="eyebrow">Endpoint summary</p>
            <h2>
              {nAbove > 0
                ? `${nAbove} of ${nScored} endpoint ${nScored === 1 ? "model is" : "models are"} above threshold`
                : "No endpoint model is above its threshold"}
            </h2>
            <p>
              Each score reflects an endpoint-associated expression pattern. Model signal score —
              not a calibrated probability of a real-world outcome.
            </p>
          </div>
          <span className="summary-status">
            <span
              className={`status-dot ${nAbove > 0 ? "status-dot-coral" : ""}`}
              aria-hidden
            />
            {nAbove} of {nScored} above threshold
          </span>
        </section>

        {runError != null && (
          <div className="no-signature" role="alert">
            <strong>The analysis request failed.</strong>
            <p>The models could not be run for this signature. Try again or adjust the input.</p>
          </div>
        )}

        {runError == null && allFailed && (
          <div className="no-signature" role="alert">
            <strong>No compatible endpoint completed successfully.</strong>
            <p>Each endpoint failure is isolated below. Use its request ID when asking an administrator for help.</p>
          </div>
        )}

        <div className="endpoint-results">
          {signals.map((s) => (
            <EndpointResultCard
              key={s.endpoint_id}
              signal={s}
              signature={input.signature}
              compatibility={input.parse.compatibility.find(
                (item) => item.endpoint_id === s.endpoint_id,
              )}
              onEvidence={onEvidence}
            />
          ))}
        </div>
      </div>

      <aside className="overview-aside">
        <section>
          <p className="aside-label">Input</p>
          <dl className="metadata-list">
            <div>
              <dt>Source</dt>
              <dd>{input.kind === "file" ? "Uploaded file" : input.kind === "catalogue" ? "Public measured signature" : input.kind === "reference" ? "Aggregated reference profile" : "Pasted signature"}</dd>
            </div>
            <div><dt>Value type</dt><dd>{input.valueTypeLabel ?? input.inputValueType.replace(/_/g, " ")}</dd></div>
            {input.referenceComparison && <div><dt>Reference comparison</dt><dd>{input.referenceComparison}</dd></div>}
            {input.cellModels?.length ? <div><dt>Cell models</dt><dd>{input.cellModels.join("; ")}</dd></div> : null}
            <div>
              <dt>Genes provided</dt>
              <dd>{Object.keys(input.signature).length}</dd>
            </div>
            <div>
              <dt>Compatible endpoints</dt>
              <dd>{input.parse.compatible_endpoint_ids.length} / {input.parse.compatibility.length}</dd>
            </div>
          </dl>
        </section>
        <section>
          <p className="aside-label">Similar measured signatures</p>
          <p className="mini-reference-copy">
            Compare this response with named public reference signatures. Similarity is descriptive
            context, not a prediction.
          </p>
          <button className="detail-link" onClick={onReference}>
            View similar signatures
          </button>
        </section>
        <section className="limitations-summary">
          <p className="aside-label">Interpretation boundary</p>
          <p>
            This screen reports experimental model signals, not biological mechanism or endocrine
            safety. Review each endpoint&rsquo;s limitations before use.
          </p>
        </section>
      </aside>
    </div>
  );
}

function EndpointResultCard({
  signal,
  signature,
  compatibility,
  onEvidence,
}: {
  signal: EndpointSignal;
  signature: Signature;
  compatibility?: EndpointCompatibility;
  onEvidence: () => void;
}) {
  if (signal.result == null) {
    return (
      <article className="endpoint-result">
        <div className="endpoint-result-top">
          <span className="endpoint-code code-generic">{signal.endpoint_id}</span>
          <span className="status-chip status-note">Not available</span>
        </div>
        <h3>{signal.biological_target}</h3>
        <p className="endpoint-result-note">
          This endpoint could not score the signature. The other endpoints are unaffected.
        </p>
        {signal.error != null && <ErrorNotice error={signal.error} />}
        {compatibility && (
          <p className="endpoint-result-note">
            Input compatibility: {compatibility.n_matched} of {compatibility.n_schema_genes} required
            genes matched.
          </p>
        )}
        <button className="detail-link" onClick={onEvidence}>
          View endpoint evidence
        </button>
      </article>
    );
  }

  const r = signal.result;
  const above = r.call;
  const score = predictionScore(r);
  const pct = Math.max(0, Math.min(100, Math.round(score * 100)));
  const thresholdPct = Math.max(0, Math.min(100, Math.round(r.threshold * 100)));

  return (
    <article className={`endpoint-result ${above ? "endpoint-positive" : ""}`}>
      <div className="endpoint-result-top">
        <span className="endpoint-code code-generic">{signal.endpoint_id}</span>
        {/* Above-threshold = warm SIGNAL chip (attention, not hazard). Below = neutral, never green. */}
        <span className={`status-chip ${above ? "status-signal" : "status-neutral"}`}>
          {above ? "Above threshold" : "Below threshold"}
        </span>
      </div>
      <h3>{signal.biological_target}</h3>
      <span className="endpoint-status-label">{r.limitations.status}</span>
      <p>Endpoint signal score</p>
      <div className="score-row">
        <strong className="tabular">{score.toFixed(2)}</strong>
        <span>Threshold {r.threshold.toFixed(2)}</span>
      </div>
      <div className={`score-track ${above ? "" : "score-track-low"}`}>
        <span style={{ width: `${pct}%` }} />
        <i style={{ left: `${thresholdPct}%` }} />
      </div>
      <p className="endpoint-interpretation">
        {above
          ? "Strong endpoint-associated expression pattern."
          : "Expression pattern below the registered endpoint threshold."}
      </p>
      <p className="score-disclaimer">
        Model signal score — not a calibrated probability of a real-world outcome.
      </p>
      <EndpointCardClues endpointId={signal.endpoint_id} signature={signature} />
      <details className="technical-disclosure endpoint-technical">
        <summary>Model and provenance details</summary>
        <dl className="technical-grid">
          <div><dt>Input fit</dt><dd>{compatibility ? `${compatibility.n_matched} / ${compatibility.n_schema_genes} required genes` : "Technical schema met"}</dd></div>
          <div><dt>Model version</dt><dd>{signal.model_version || "Not recorded"}</dd></div>
          <div><dt>Data provenance</dt><dd>{signal.source_refs?.join(", ") || "Not recorded"}</dd></div>
          <div><dt>Input handling</dt><dd>{r.standardized_input ? "Standardized by the registered model pipeline" : "No standardization applied"}</dd></div>
        </dl>
      </details>
      <button className="detail-link" onClick={onEvidence}>
        View endpoint evidence
      </button>
    </article>
  );
}

function EndpointCardClues({
  endpointId,
  signature,
}: {
  endpointId: string;
  signature?: Signature;
}) {
  if (!signature) return null;
  return <EndpointCardCluesLoaded endpointId={endpointId} signature={signature} />;
}

function EndpointCardCluesLoaded({ endpointId, signature }: { endpointId: string; signature: Signature }) {
  const explanation = useAsync(() => api.explain(endpointId, signature, 3), [endpointId, signature]);
  const pathways = useAsync(() => api.interpretPathways(endpointId, signature), [endpointId, signature]);
  if (!explanation.data && !pathways.data?.pathways.length) return null;
  return (
    <div className="endpoint-clues">
      {explanation.data && (
        <div><span>Leading contributors</span><strong>{explanation.data.top_contributors.slice(0, 3).map((item) => item.gene).join(" · ")}</strong></div>
      )}
      {pathways.data?.pathways.length ? (
        <div><span>Biological clues</span><strong>{pathways.data.pathways.slice(0, 2).map((item) => item.name).join(" · ")}</strong></div>
      ) : null}
    </div>
  );
}

function ReportTab({
  input,
  signals,
  nAbove,
}: {
  input: PreparedInput;
  signals: EndpointSignal[];
  nAbove: number;
}) {
  const scored = signals.filter((s) => s.result != null);
  return (
    <div className="report-layout">
      <aside className="report-toc">
        <p className="aside-label">Report contents</p>
        {[
          "Input and experimental context",
          "Endpoint summary",
          "Global biological response",
          "Endpoint-specific evidence",
          "Contributing genes",
          "Endpoint-specific pathways",
          "Supporting literature",
          "Similar measured signatures",
          "Limitations",
          "Model and analysis provenance",
        ].map((item, index) => (
          <button key={item} className={index === 0 ? "selected" : ""} disabled>
            <span>{String(index + 1).padStart(2, "0")}</span>
            {item}
          </button>
        ))}
      </aside>
      <article className="report-document">
        <div className="report-document-header">
          <div>
            <p>ENDOSCAN ANALYSIS REPORT</p>
            <span>Preview — export is a planned capability</span>
          </div>
          <div>
            <PlannedBadge label="Report export: planned" />
          </div>
        </div>
        <div className="report-title">
          <div>
            <p className="eyebrow">Executive summary</p>
            <h1>Transcriptomic analysis result</h1>
            <p>Measured biological response and registered experimental endpoint signals.</p>
          </div>
          <span className="summary-status">
            <span className={`status-dot ${nAbove > 0 ? "status-dot-coral" : ""}`} aria-hidden />
            {nAbove} of {scored.length} above threshold
          </span>
        </div>
        <div className="report-metrics">
          {scored.map((s) => (
            <div key={s.endpoint_id}>
              <span>{s.endpoint_id} endpoint</span>
              <strong className="tabular">{predictionScore(s.result!).toFixed(2)}</strong>
              <small>{s.result!.call ? "Above threshold" : "Below threshold"}</small>
            </div>
          ))}
        </div>
        <section className="report-section">
          <h2>Input and compatibility</h2>
          <div className="report-info-grid">
            <div>
              <span>Signature</span>
              <strong>{input.title}</strong>
            </div>
            <div>
              <span>Genes provided</span>
              <strong>{Object.keys(input.signature).length}</strong>
            </div>
            {input.parse.preview && (
              <div>
                <span>Compatible endpoints</span>
                <strong>{input.parse.compatible_endpoint_ids.length} / {input.parse.compatibility.length}</strong>
              </div>
            )}
            <div>
              <span>Endpoints assessed</span>
              <strong>{scored.length}</strong>
            </div>
          </div>
        </section>
        <footer className="report-footer">
          <strong>Experimental research use only</strong>
          <p>
            A downloadable, reproducible report (HTML / PDF / JSON with full provenance, model cards
            and limitations) is a planned capability and is not generated yet. EndoScan does not
            provide clinical, diagnostic, regulatory or safety conclusions.
          </p>
        </footer>
      </article>
    </div>
  );
}
