// Validation step (ported from prototype-v2's input check). Driven by REAL data:
//   - gene coverage comes from the upload parse preview when present (matched / missing / extra vs
//     the model's landmark schema); for pasted/demo input we show the provided gene count and note
//     the server makes the final schema decision on run.
//   - model compatibility lists the endpoints the API actually returned (/endpoints) — never a
//     hardcoded ER/AR pair. The final gene-set validation is the server's, per endpoint.
// Experimental context fields are shown for the record but are NOT sent to the model (the models
// consume the transcriptomic signature only); this is stated plainly so nothing is implied.

import type { PreparedInput } from "../../pages/Analyze";

export function ValidationStep({
  input,
  onBack,
  onRun,
}: {
  input: PreparedInput;
  onBack: () => void;
  onRun: () => void;
}) {
  const preview = input.parse.preview;
  const compatibility = input.parse.compatibility;
  const compatible = compatibility.filter((item) => item.compatible);
  const provided = Object.keys(input.signature).length;

  return (
    <section className="validation-layout">
      <div className="validation-main">
        <div className="section-title-row">
          <div>
            <p className="eyebrow">Input check</p>
            <h2>Confirm what will be analyzed</h2>
          </div>
          <span className="validation-state">
            <span className="status-dot" aria-hidden />
            {compatible.length ? "Ready to run" : "No compatible endpoints"}
          </span>
        </div>

        <div className="input-summary">
          <div className="file-mark">
            {input.kind === "file" ? "CSV" : input.kind === "demo" ? "DEMO" : input.kind === "catalogue" ? "PUBLIC" : "JSON"}
          </div>
          <div>
            <strong>{input.title}</strong>
            <span>{input.subtitle}</span>
          </div>
          <button className="button outline" onClick={onBack}>
            Change input
          </button>
        </div>

        <div className="check-section">
          <div className="check-heading">
            <div>
              <span className="check-number">1</span>
              <div>
                <h3>Gene coverage</h3>
                <p>Coverage is checked against the required landmark schema.</p>
              </div>
            </div>
            <span className="status-chip status-good">{preview.n_detected} genes parsed</span>
          </div>
          <p className="check-plain">
            {provided} gene values provided. Compatibility below is calculated independently for
            every endpoint&rsquo;s registered feature schema.
          </p>
        </div>

        <div className="check-section">
          <div className="check-heading">
            <div>
              <span className="check-number">2</span>
              <div>
                <h3>Experimental context</h3>
                <p>Recorded for your notes. Models consume the signature only, not these fields.</p>
              </div>
            </div>
            <span className="status-chip status-note">Optional</span>
          </div>
          <div className="context-grid">
            <label>
              <span>Cell context</span>
              <input placeholder="e.g. MCF-7" />
            </label>
            <label>
              <span>Exposure time</span>
              <input placeholder="e.g. 24 h" />
            </label>
            <label>
              <span>Dose</span>
              <input placeholder="e.g. 10 µM" />
            </label>
            <label>
              <span>Value scale</span>
              <select defaultValue="log2 fold change">
                <option>log2 fold change</option>
                <option>z-score</option>
                <option>Normalized expression</option>
              </select>
            </label>
          </div>
        </div>
      </div>

      <aside className="compatibility-panel">
        <p className="eyebrow">Model compatibility</p>
        <h2>
          {compatible.length} of {compatibility.length} endpoint model{compatibility.length === 1 ? "" : "s"} compatible
        </h2>
        <p>Only compatible endpoints will be submitted for analysis.</p>
        <div className="compatibility-list">
          {compatibility.map((e) => (
            <div key={e.endpoint_id}>
              <span className="endpoint-code code-generic">
                {e.endpoint_id}
              </span>
              <span>
                <strong>{e.biological_target}</strong>
                <small>{e.n_matched}/{e.n_schema_genes} matched · {e.n_missing} missing · {e.n_extra} extra</small>
              </span>
              <b>{e.compatible ? "Compatible" : "Incompatible"}</b>
            </div>
          ))}
        </div>
        {compatible.length === 0 && (
          <div className="no-signature" role="status">
            <strong>No registered endpoint can use this signature.</strong>
            <p>Review the per-endpoint missing and extra gene counts, then choose another input.</p>
          </div>
        )}
        <div className="model-note">
          <span className="status-dot status-dot-amber" aria-hidden />
          <p>
            Scores indicate similarity to learned endpoint-associated patterns. They are not
            clinical, regulatory or safety conclusions.
          </p>
        </div>
        <button
          className="button primary full-button"
          disabled={compatible.length === 0}
          onClick={onRun}
        >
          Run {compatible.length} compatible endpoint model{compatible.length === 1 ? "" : "s"}
        </button>
        <button className="button quiet full-button" onClick={onBack}>
          Back
        </button>
      </aside>
    </section>
  );
}
